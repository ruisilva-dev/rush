.PHONY: up down logs test

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f

test:
	docker compose -f docker-compose.test.yml run --build --rm test

test-clean:
	docker compose -f docker-compose.test.yml run --build --rm test
	docker compose -f docker-compose.test.yml down test test_postgres

migrate:
	docker compose exec web alembic upgrade head

revision:
	docker compose exec -u $(shell id -u):$(shell id -g) web alembic revision --autogenerate -m "$(m)"
