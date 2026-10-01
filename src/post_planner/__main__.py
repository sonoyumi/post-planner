"""Точка входа: `python -m post_planner` или команда `post-planner`."""


def greet(name: str) -> str:
    return f"Привет, {name}!"


def main() -> None:
    print(greet("мир"))


if __name__ == "__main__":
    main()
