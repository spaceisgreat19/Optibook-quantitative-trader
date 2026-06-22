# Quantitative Trading Bot (Optibook Prototype)

## Overview

This project is a **quantitative trading system prototype** built for the *Imperial x Optiver trading challenge* using the Optibook simulation environment.

It implements a modular multi-strategy trading engine designed to trade, hedge, and manage risk under strict exchange constraints such as position limits, order limits, and rate limits.

The system runs in a continuous tick-based loop, dynamically reacting to market data and internal risk signals.

---

# Trading Strategies

## 1. Market Making

The market making module provides continuous liquidity across selected equities (e.g. AAPL, NVDA, TSLA).

**Key ideas:**
- Quotes both bid and ask prices around mid-price
- Adjusts prices based on inventory exposure (skewing)
- Dynamically reduces size when position becomes large
- Avoids excessive re-quoting by only updating when price movement is meaningful
- Maintains minimum spread constraints to avoid adverse selection

---

## 2. Dual Listing Arbitrage

This strategy exploits price differences between dual-listed instruments.

**How it works:**
- Compares primary and dual-listed assets in real time
- Executes simultaneous buy/sell trades when price divergence exceeds threshold
- Requires minimum profit margin before execution
- Uses execution locking to ensure both legs of the trade complete consistently

---

## 3. Balance Trading

This module trades relative mispricing across correlated instruments and futures baskets.

**Key ideas:**
- Compares related instruments within defined baskets
- Identifies spread inefficiencies (net edge)
- Executes trades when spread exceeds configurable threshold
- Filters out unrealistic price dislocations
- Respects position and exposure limits across baskets

---

## 4. Dirty Hedge Strategy

A basket-based hedging strategy that builds synthetic long/short positions.

**Key ideas:**
- Constructs weighted long and short portfolios
- Trades only when sufficient edge exists between baskets
- Controls net exposure using delta limits
- Prevents over-hedging by tracking basket-level exposure

---

# System Features

## Shadow Position Tracking

A local "shadow" position system is maintained to:
- Predict positions before exchange confirmation
- Prevent accidental breaches of position limits
- Enable safe order simulation before execution

---

## Rate Limiting System

A token-bucket rate limiter ensures:
- Exchange call limits are never exceeded
- Separation of read vs write operations
- Safe throttling of order submission bursts

---

## Outstanding Order Tracking

The system tracks active orders per instrument to:
- Avoid exceeding outstanding order limits
- Prevent duplicate or redundant orders
- Manage liquidity exposure efficiently

---

## Delta Risk Monitor

A risk control module that:
- Tracks directional exposure per instrument
- Triggers reduction trades when exposure stays too high
- Applies a grace period before forced unwinding

---

## Leg Lock Protection

Ensures paired trades remain balanced:
- Tracks execution of multi-leg trades
- Detects partial fills or imbalance
- Automatically hedges unintended exposure

---

## Execution Architecture

The trading loop follows this structure each tick:

1. Fetch market data and positions
2. Update shadow position model
3. Run risk controls (delta monitor + leg lock)
4. Execute market making (priority liquidity provision)
5. Run arbitrage strategies
6. Run relative value / balance trading
7. Run hedge strategy
8. Wait for next tick

---

# System Summary

This bot is designed as a **multi-layered quantitative trading system** combining:

- Market making for consistent liquidity provision  
- Arbitrage for low-risk profit extraction  
- Relative value trading for inefficiency capture  
- Hedging for exposure control  
- Strong internal risk management systems  

# Author

Nathaniel Darren Lim
