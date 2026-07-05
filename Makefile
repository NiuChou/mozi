# 墨子 · 一键命令
.PHONY: bootstrap dev backend frontend test lint ci build stop clean

BE := backend
FE := frontend
PORT ?= 8000

bootstrap:        ## 装后端 + 前端依赖 + 启用本地 CI 钩子
	cd $(BE) && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
	cd $(FE) && npm install
	@git rev-parse --git-dir >/dev/null 2>&1 && git config core.hooksPath .githooks && echo "已启用 pre-push 本地 CI 钩子" || true

backend:          ## 起后端网关 (Python 服务)
	cd $(BE) && .venv/bin/python -m uvicorn mozi_backend.main:app --host 127.0.0.1 --port $(PORT) --reload

frontend:         ## 起前端 (墨子·Chat)
	cd $(FE) && npm run dev

dev:              ## 并行起 后端(8000) + 前端(5173)
	@echo "后端 :$(PORT)  前端 :5173  (Ctrl-C 停止)"
	@( cd $(BE) && .venv/bin/python -m uvicorn mozi_backend.main:app --port $(PORT) ) & \
	 ( cd $(FE) && npm run dev ) & wait

test:             ## 端到端冒烟测试 (smoke 48 项)
	cd $(BE) && .venv/bin/python smoke_test.py

lint:             ## 后端 Lint (ruff)
	cd $(BE) && .venv/bin/ruff check .

ci:               ## 本地 CI 全流水线 (lint + 冒烟 + 前端构建)
	./scripts/ci.sh

build:            ## 前端生产构建
	cd $(FE) && npm run build

stop:             ## 停掉占用 8000/5173 的进程
	@for p in $(PORT) 5173; do pid=$$(lsof -ti tcp:$$p 2>/dev/null); [ -n "$$pid" ] && kill $$pid && echo "stopped :$$p" || true; done

clean:            ## 清本地库 + 构建产物
	rm -rf $(BE)/data $(FE)/dist
