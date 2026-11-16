# 光学制造过程数据湖
本服务使用 FastAPI 和 SQLite 记录光学制造的来源合同、原始批次、规范事件、镜片谱系和冻结数仓版本。仓库里的工厂与事件均为合成演示数据。
先运行 python3 -m optic_lake.demo --fixture fixtures/demo.json 回放完整业务流程。启动接口可执行 uvicorn optic_lake.api:app --host 127.0.0.1 --port 8000，数据库路径由 OPTIC_LAKE_DB 指定。
来源合同限定字段映射和有效期；坏行进入隔离区，合格行仍被接受。谱系链接限制上游消耗数量。快照准备后若来源修订或发布头变化，旧快照不得发布；已发布版本保持不可变。
业务测试命令：python3 -m unittest discover -s tests -v
构建检查命令：python3 -m compileall -q optic_lake
验收命令：python3 -m optic_lake.demo --fixture fixtures/demo.json