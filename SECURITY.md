# Security Policy

## 1. 私下报告

请使用 GitHub 仓库的 `Security` -> `Advisories` -> `Report a vulnerability` 私下提交漏洞。公开 Issue 不适合包含漏洞细节、凭证或个人信息。

报告应包含受影响版本、复现步骤、影响范围和已知缓解方式。维护者确认前，请勿公开利用代码或真实凭证。

## 2. 凭证

不要提交 API key、token、私钥、Cookie、`.env`、本机 provider 配置、云账号信息、请求头或包含凭证的日志。发生泄露后应立即在供应商侧撤销或轮换；从 Git 删除文件不能使旧凭证失效。

## 3. Agent 执行边界

`ar-runtime` 能生成代码和执行 shell 命令。请在隔离、可丢弃的工作区运行，使用最小权限凭证，限制网络、挂载、GPU 和费用额度。不要把客户数据、个人信息或受保密义务约束的材料直接交给 Agent。

当前 Alpha 版本不适合无人值守的生产环境。
