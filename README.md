# post-planner

Короткое описание: что делает проект и зачем.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # заполнить своими значениями
post-planner           # или: python -m post_planner
```

## Тесты и линтер

```bash
pytest
ruff check .
```

## Структура

```
src/post_planner/   код
tests/              тесты
```
