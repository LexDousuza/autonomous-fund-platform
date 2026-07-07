# Autonomous Quantitative Trading Platform (AQTP)

Version: 1.0

---

# Mission

AQTP is an institutional-grade quantitative trading platform designed for Indian financial markets.

The platform is intended to evolve from a research environment into a fully autonomous execution platform capable of:

- Strategy research
- Strategy development
- Historical backtesting
- Paper trading
- Live trading
- Portfolio management
- Risk management
- Performance analytics
- Continuous optimization

The long-term objective is to create a production-quality quantitative investment platform with clear separation between research, simulation, execution, and operational workflows.

---

# Core Philosophy

The repository is treated as a long-lived software product.

Every component should be:

- modular
- testable
- deterministic
- auditable
- configurable
- reusable
- documented

No component should depend on hidden assumptions.

Every trading decision must be reproducible from stored inputs.

---

# Primary Markets

Primary target:

- NSE Cash Equities
- BSE Cash Equities

Future expansion may include:

- NSE Futures
- NSE Options
- Commodity markets
- International markets

No architecture decisions should prevent future expansion.

---

# Development Stages

Stage 1

Research

The strategy is developed entirely from historical data.

No live execution.

---

Stage 2

Backtesting

Strategies are validated against historical datasets.

Only reproducible research is accepted.

---

Stage 3

Paper Trading

Strategies execute automatically using simulated capital.

No real capital is used.

---

Stage 4

Live Trading

Strategies execute using real brokerage integrations only after successful validation.

Live deployment requires explicit user approval.

---

# Platform Principles

The platform is divided into independent subsystems.

Research

↓

Strategy

↓

Risk

↓

Execution

↓

Portfolio

↓

Reporting

↓

Monitoring

Each subsystem must remain independent.

Subsystems communicate through clearly defined interfaces.

Direct coupling between unrelated modules is discouraged.

---

# AI Development Philosophy

Claude is considered a senior quantitative software engineer working inside this repository.

Claude should prioritize:

correctness

maintainability

clarity

reproducibility

performance

over short-term convenience.

Claude should improve architecture whenever appropriate instead of only satisfying the immediate request.

Whenever architectural improvements are suggested, they should preserve backward compatibility whenever reasonably possible.

---

# Repository Architecture

The repository is organized into independent layers.

Each layer has a single responsibility.

Dependencies should always flow downward.

Applications may depend on core modules.

Core modules must never depend on applications.

The preferred dependency direction is:

Apps
↓

Services
↓

Core

Configuration and data are external resources and should never contain business logic.

---

# Directory Responsibilities

## apps/

Contains executable entry points.

Examples:

- paper trader
- live trader
- backtester
- reporting
- dashboards

Applications should orchestrate components.

Applications should not contain trading logic.

---

## core/

Contains reusable business logic.

Examples:

- strategy interfaces
- broker interfaces
- execution
- portfolio
- risk
- analytics

Core modules should remain reusable.

Core should never import from apps.

---

## config/

Contains configuration only.

Examples:

- strategy parameters
- broker configuration
- risk limits
- allocation rules
- trading universes

Configuration should remain declarative.

Configuration must never contain executable logic.

---

## data/

Contains generated or imported datasets.

Examples:

- historical bars
- reports
- audit logs
- paper trading logs
- backtest outputs

Data files must never contain source code.

---

## docs/

Contains architecture documentation.

Contains research notes.

Contains operational documentation.

Contains design decisions.

---

## tests/

Contains automated tests.

Every production module should eventually have corresponding tests.

---

# Engineering Principles

The codebase follows the following principles.

Single Responsibility Principle

One module.

One responsibility.

Small interfaces.

Loose coupling.

High cohesion.

---

Composition over inheritance whenever practical.

Avoid deep inheritance hierarchies.

---

Prefer explicit configuration over hidden defaults.

---

Avoid global mutable state.

---

Prefer dependency injection.

---

Business logic must remain deterministic.

Given identical inputs,

the platform should produce identical outputs.

---

# Import Rules

Allowed

apps

↓

core

Allowed

apps

↓

config

Allowed

apps

↓

data

Not allowed

core

↓

apps

Not allowed

core

↓

tests

Not allowed

core

↓

notebooks

---

# File Size Guidelines

Target file sizes.

Interface files

<150 lines

Business logic

150–400 lines

Large engines

400–700 lines

If a file grows beyond approximately 700 lines,

consider splitting it.

---

# Documentation Requirements

Every public class should have a docstring.

Every public function should have a docstring.

Complex algorithms should explain why,

not only what.

Comments should document intent,

not obvious implementation details.

---

# Logging Philosophy

Never use print() inside production business logic.

Production modules should use structured logging.

Applications may print summaries for users.

Detailed execution information should be written to logs.

---

# Error Handling

Never silently ignore exceptions.

Errors should either:

be handled,

or propagated with additional context.

Fail loudly during development.

Fail predictably in production.
