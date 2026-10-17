"""适配器层：端口的真实实现 + 假实现 + 未实现占位。

* 真实：`SqliteStore`（M1）、`CommentNotifier`（M1 起可用）、
  `GitLabClient` / `GitCli` / `CodexCli`（M2 / M3）；
* 假：`FakeSCM` / `FakeVCS` / `FakeAgent` / `FakeWorkspace` / `FakeNotifier`，
  供 CI 里的端到端集成测试使用（不需要 GitLab、不需要 LLM）；
* 占位：`Unavailable*`，在真实适配器落地前让网关保持"能启动、但任务明确失败"。
"""
