import datetime as dt
import time
import logging
import collections
from optibook.synchronous_client import Exchange


# exchange limits
POSITION_LIMIT    = 100
OUTSTANDING_LIMIT = 200
DELTA_SOFT_LIMIT  = 100
DELTA_GRACE_SECS  = 8.0


# rate limiter tuned for exchange cap
# only insert/delete/amend count toward limits
class RateLimiter:
    def __init__(self, max_calls: int = 23, window: float = 1.0):
        self.max_calls  = max_calls
        self.window     = window
        self.timestamps = collections.deque()

    def throttle(self):
        now = time.monotonic()
        while self.timestamps and self.timestamps[0] <= now - self.window:
            self.timestamps.popleft()

        if len(self.timestamps) >= self.max_calls:
            sleep_for = self.window - (now - self.timestamps[0]) + 0.002
            if sleep_for > 0:
                time.sleep(sleep_for)

            now = time.monotonic()
            while self.timestamps and self.timestamps[0] <= now - self.window:
                self.timestamps.popleft()

        self.timestamps.append(time.monotonic())

    @property
    def remaining(self) -> int:
        now = time.monotonic()
        while self.timestamps and self.timestamps[0] <= now - self.window:
            self.timestamps.popleft()
        return self.max_calls - len(self.timestamps)


rl = RateLimiter(max_calls=23)


# instruments and market making config
MM_TICKERS = ["AAPL", "NVDA", "TSLA"]

MM_CONFIG = {
    "AAPL": (0.20, 100, 15),
    "NVDA": (0.20, 100, 15),
    "TSLA": (0.30, 100, 10),
}

# only re-quote when price moves enough to matter
MM_MIN_PRICE_DELTA  = 0.10
MM_SLOT_BUDGET_PAIRS = 3
MM_RESET_EVERY      = 15
MM_RESET_OFFSETS    = {"AAPL": 0, "NVDA": 5, "TSLA": 10}


# dual listing arbitrage setup
DUAL_LISTING_PAIRS = [
    {"primary": "SAP",  "dual": "SAP_DUAL"},
    {"primary": "ASML", "dual": "ASML_DUAL"},
]

DUAL_LISTING_TRADE_VOL  = 5
DUAL_LISTING_MIN_PROFIT = 0.05


# relative pricing baskets used for stat arb
COMPARABLE_PAIRS = {
    "ASML_UNIVERSE": [
        ("ASML", "ASML_DUAL"),
        ("ASML_202609_F", "ASML_202612_F"),
        ("ASML_202609_F", "ASML_202703_F"),
        ("ASML_202612_F", "ASML_202703_F"),
    ],
    "SAP_UNIVERSE": [
        ("SAP", "SAP_DUAL"),
    ],
    "OB5X_UNIVERSE": [
        ("OB5X_202609_F", "OB5X_202612_F"),
        ("OB5X_202609_F", "OB5X_202703_F"),
        ("OB5X_202612_F", "OB5X_202703_F"),
    ],
}


CARRY_PAIRS = {
    ("ASML_202609_F", "ASML_202612_F"),
    ("ASML_202609_F", "ASML_202703_F"),
    ("ASML_202612_F", "ASML_202703_F"),
    ("OB5X_202609_F", "OB5X_202612_F"),
    ("OB5X_202609_F", "OB5X_202703_F"),
    ("OB5X_202612_F", "OB5X_202703_F"),
}


BALANCE_MIN_NET_PROFIT    = 0.05
BALANCE_CALENDAR_MIN_EDGE = 0.50
BALANCE_MAX_SANE_EDGE     = 3.0
BALANCE_MAX_BASKET_DELTA  = 200
BALANCE_MAX_TRADE_SIZE    = 100


DIRTY_HEDGE_CONFIG = {
    "ASML_HEDGE": {
        "long_basket":  [("ASML", 1.0), ("ASML_DUAL", 1.0)],
        "short_basket": [("ASML_202612_F", 1.0)],
        "min_profit":   0.10,
        "max_delta":    200,
    },
}


# connect to exchange
exchange = Exchange()
exchange.connect()
logging.getLogger("client").setLevel("ERROR")


# track outstanding volume for safety checks
_outstanding_volume: dict = collections.defaultdict(int)

def _outstanding_headroom(inst: str) -> int:
    return max(0, OUTSTANDING_LIMIT - _outstanding_volume[inst])

def _register_insert(inst: str, vol: int):
    _outstanding_volume[inst] += vol

def _register_delete(inst: str, vol: int):
    _outstanding_volume[inst] = max(0, _outstanding_volume[inst] - vol)


# shadow position model used for internal risk tracking
class ShadowPositions:
    def __init__(self, snapshot: dict):
        self._pos = dict(snapshot)

    def get(self, inst: str, default: int = 0) -> int:
        return self._pos.get(inst, default)

    def apply(self, inst: str, volume: int, side: str):
        delta = volume if side == "bid" else -volume
        self._pos[inst] = self._pos.get(inst, 0) + delta

    def would_breach(self, inst: str, volume: int, side: str) -> bool:
        new_pos = self._pos.get(inst, 0) + (volume if side == "bid" else -volume)
        return abs(new_pos) > POSITION_LIMIT


shadow: ShadowPositions = ShadowPositions({})


# exchange wrappers
def get_book(inst):
    return exchange.get_last_price_book(inst)

def get_positions():
    return exchange.get_positions()


def insert_order(inst: str, price: float, vol: int, side: str,
                 order_type: str = "ioc") -> bool:

    if shadow.would_breach(inst, vol, side):
        print(f"[BLOCKED] {inst} {side} {vol}")
        return False

    if order_type == "limit":
        vol = min(vol, min(_outstanding_headroom(inst), POSITION_LIMIT))
        if vol < 1:
            return False

    rl.throttle()
    exchange.insert_order(inst, price=price, volume=vol, side=side, order_type=order_type)
    shadow.apply(inst, vol, side)

    if order_type == "limit":
        _register_insert(inst, vol)

    return True


def delete_order(inst: str, order_id, vol: int = 0):
    rl.throttle()
    exchange.delete_order(inst, order_id=order_id)
    if vol:
        _register_delete(inst, vol)


# ensures paired trades stay balanced
class LegLockGuard:
    _pending: list = []

    @classmethod
    def register(cls, buy_inst: str, sell_inst: str, volume: int,
                 pre_pos_buy: int, pre_pos_sell: int):

        cls._pending.append((
            time.monotonic() + 0.35,
            buy_inst, sell_inst, volume, pre_pos_buy, pre_pos_sell,
        ))

    @classmethod
    def run_checks(cls, positions: dict):
        now = time.monotonic()
        pending = []

        for entry in cls._pending:
            check_after, buy_inst, sell_inst, expected_vol, pre_buy, pre_sell = entry

            if now < check_after:
                pending.append(entry)
                continue

            actual_buy_fill  =  (positions.get(buy_inst,  0) - pre_buy)
            actual_sell_fill = -(positions.get(sell_inst, 0) - pre_sell)
            net_long = actual_buy_fill - actual_sell_fill

            if abs(net_long) < 1:
                print(f"[LEG OK] {buy_inst}/{sell_inst}")
                continue

            print(f"[LEG IMBALANCE] {net_long:+d} hedging")

            if net_long > 0:
                book = get_book(buy_inst)
                if book and book.bids:
                    insert_order(buy_inst, book.bids[0].price, abs(net_long), "ask", "ioc")

            elif net_long < 0:
                book = get_book(sell_inst)
                if book and book.asks:
                    insert_order(sell_inst, book.asks[0].price, abs(net_long), "bid", "ioc")

        cls._pending = pending


# soft risk reducer for large directional exposure
class DeltaMonitor:
    _breach_start: dict = {}

    @classmethod
    def check(cls, positions: dict):
        now = time.monotonic()

        for inst, pos in positions.items():
            if abs(pos) >= DELTA_SOFT_LIMIT:
                cls._breach_start.setdefault(inst, now)
                if now - cls._breach_start[inst] >= DELTA_GRACE_SECS:
                    cls._reduce(inst, pos)
            else:
                cls._breach_start.pop(inst, None)

    @classmethod
    def _reduce(cls, inst: str, pos: int):
        book = get_book(inst)
        if not book:
            return

        if pos > 0 and book.bids:
            vol = min(abs(pos) // 10 + 1, BALANCE_MAX_TRADE_SIZE)
            if insert_order(inst, book.bids[0].price, vol, "ask", "ioc"):
                cls._breach_start[inst] = time.monotonic()

        elif pos < 0 and book.asks:
            vol = min(abs(pos) // 10 + 1, BALANCE_MAX_TRADE_SIZE)
            if insert_order(inst, book.asks[0].price, vol, "bid", "ioc"):
                cls._breach_start[inst] = time.monotonic()


# simple market making logic with position skew
class MarketMaker:
    def execute(self, tick: int):
        pairs_used = 0

        for inst in MM_TICKERS:
            if pairs_used >= MM_SLOT_BUDGET_PAIRS:
                break

            if rl.remaining < 2:
                break

            cfg = MM_CONFIG[inst]
            pairs_used += self._quote(inst, cfg, tick,
                                      MM_SLOT_BUDGET_PAIRS - pairs_used)

    def _quote(self, inst: str, cfg: tuple, tick: int, budget_pairs: int) -> int:
        min_spread, max_pos, std_size = cfg
        pairs_used = 0

        offset = MM_RESET_OFFSETS.get(inst, 0)
        if (tick - offset) % MM_RESET_EVERY == 0:
            for order_id, o in exchange.get_outstanding_orders(inst).items():
                if pairs_used >= budget_pairs:
                    break
                delete_order(inst, order_id, o.volume)
                pairs_used += 1

        book = get_book(inst)
        if not (book and book.bids and book.asks):
            return pairs_used

        pos = shadow.get(inst)

        best_bid = book.bids[0].price
        best_ask = book.asks[0].price

        if best_ask - best_bid < min_spread:
            mid = (best_bid + best_ask) / 2
            best_bid = round(mid - min_spread / 2, 1)
            best_ask = round(mid + min_spread / 2, 1)

        skew = (-0.10 if pos > max_pos * 0.25 else
                 0.10 if pos < -max_pos * 0.25 else 0.0)

        bid_px = round(best_bid + skew, 1)
        ask_px = round(best_ask + skew, 1)

        own = exchange.get_outstanding_orders(inst)
        own_bids = [o for o in own.values() if o.side == "bid"]
        own_asks = [o for o in own.values() if o.side == "ask"]

        need_bid = (
            not own_bids
            or abs(own_bids[0].price - bid_px) >= MM_MIN_PRICE_DELTA
            or own_bids[0].volume != std_size
        )

        need_ask = (
            not own_asks
            or abs(own_asks[0].price - ask_px) >= MM_MIN_PRICE_DELTA
            or own_asks[0].volume != std_size
        )

        if need_bid and rl.remaining >= 2:
            for o in own_bids:
                delete_order(inst, o.order_id, o.volume)

            if std_size > 0 and not shadow.would_breach(inst, std_size, "bid"):
                insert_order(inst, bid_px, std_size, "bid", "limit")
                print(f"[{inst}] BID {std_size} @ {bid_px:.1f}")

        if need_ask and rl.remaining >= 2:
            for o in own_asks:
                delete_order(inst, o.order_id, o.volume)

            if std_size > 0 and not shadow.would_breach(inst, std_size, "ask"):
                insert_order(inst, ask_px, std_size, "ask", "limit")
                print(f"[{inst}] ASK {std_size} @ {ask_px:.1f}")

        return pairs_used


# dual listing arbitrage
class DualListingTrader:
    def execute(self, traded_pairs: set) -> set:
        for pair in DUAL_LISTING_PAIRS:
            if rl.remaining < 2:
                break

            p = pair["primary"]
            d = pair["dual"]

            book_p = get_book(p)
            book_d = get_book(d)

            if not (book_p and book_d and book_p.bids and book_p.asks and book_d.bids and book_d.asks):
                continue

            vol = DUAL_LISTING_TRADE_VOL
            key = frozenset([p, d])

            if (book_d.bids[0].price - book_p.asks[0].price) >= DUAL_LISTING_MIN_PROFIT:
                if insert_order(p, book_p.asks[0].price, vol, "bid", "ioc") and \
                   insert_order(d, book_d.bids[0].price, vol, "ask", "ioc"):
                    print(f"[DUAL] {p} vs {d} trade")
                    traded_pairs.add(key)

            elif (book_p.bids[0].price - book_d.asks[0].price) >= DUAL_LISTING_MIN_PROFIT:
                if insert_order(d, book_d.asks[0].price, vol, "bid", "ioc") and \
                   insert_order(p, book_p.bids[0].price, vol, "ask", "ioc"):
                    print(f"[DUAL] {d} vs {p} trade")
                    traded_pairs.add(key)

        return traded_pairs


# stat arb basket trading
class BalanceTrader:
    def execute(self, traded_pairs: set):
        for basket_name, pairs in COMPARABLE_PAIRS.items():
            if rl.remaining < 2:
                break
            self._run_basket(basket_name, pairs, traded_pairs)

    def _run_basket(self, basket_name, pairs, traded_pairs):
        needed = {i for p in pairs for i in p}
        books = {i: get_book(i) for i in needed}
        books = {k: v for k, v in books.items() if v and v.bids and v.asks}

        best = None

        for a, b in pairs:
            if frozenset([a, b]) in traded_pairs:
                continue

            for buy, sell in [(a, b), (b, a)]:
                b_book, s_book = books.get(buy), books.get(sell)
                if not b_book or not s_book:
                    continue

                edge = s_book.bids[0].price - b_book.asks[0].price

                if best is None or edge > best[0]:
                    best = (edge, buy, sell)

        if best:
            edge, buy, sell = best
            print(f"[BASKET] {basket_name} edge {edge:.3f}")


# hedge logic for imbalance positions
class DirtyHedgeTrader:
    def execute(self, traded_pairs: set):
        if rl.remaining < 3:
            return

        for _, cfg in DIRTY_HEDGE_CONFIG.items():
            longs = cfg["long_basket"]
            shorts = cfg["short_basket"]

            insts = {i for i, _ in longs + shorts}
            books = {i: get_book(i) for i in insts}
            books = {k: v for k, v in books.items() if v and v.bids and v.asks}

            if len(books) < len(insts):
                continue

            long_px = sum(books[i].asks[0].price * w for i, w in longs)
            short_px = sum(books[i].bids[0].price * w for i, w in shorts)

            if short_px - long_px < cfg["min_profit"]:
                continue

            print("[HEDGE] opportunity found")


# main loop
mm = MarketMaker()
dual = DualListingTrader()
balance = BalanceTrader()
dirty = DirtyHedgeTrader()

tick = 0

while True:
    tick += 1
    print(f"\nTick {tick} {dt.datetime.now()}")

    raw_positions = get_positions()
    shadow.__init__(raw_positions)

    DeltaMonitor.check(raw_positions)
    LegLockGuard.run_checks(raw_positions)

    traded_this_tick = set()

    mm.execute(tick)
    dirty.execute(traded_this_tick)
    dual.execute(traded_this_tick)
    balance.execute(traded_this_tick)

    time.sleep(0.5)