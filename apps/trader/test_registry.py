from core.strategy.registry import registry

# Import registers the strategy automatically
import core.strategy.simple_breakout_momentum  # noqa: F401

print("Registered strategies:")

for name in registry.available():
    print("-", name)
