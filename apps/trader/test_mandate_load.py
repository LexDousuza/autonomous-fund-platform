from core.allocations.daily_mandate import load_daily_mandate


def main() -> None:
    mandate = load_daily_mandate("config/mandates/sample_mandate.yaml")

    print("Mandate loaded successfully")
    print(f"Trading date: {mandate.trading_date}")
    print(f"Reserve bucket: {mandate.buckets.reserve_bucket}")
    print(f"Intraday bucket: {mandate.buckets.intraday_bucket}")
    print(f"Total capital: {mandate.buckets.total}")
    print(f"Max daily loss: {mandate.risk_limits.max_daily_loss}")
    print(f"Max trades/day: {mandate.risk_limits.max_trades_per_day}")
    print(f"Execution mode: {mandate.execution_mode}")


if __name__ == "__main__":
    main()