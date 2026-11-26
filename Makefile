.PHONY: install backend frontend verify build

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm ci

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

# 采集量迁移上线核验：在临时 SQLite 上复演 37 -> +2 -> 39、冲突/幂等/切换/回滚/重启。
verify:
	backend/.venv/bin/python scripts/verify_migration.py
