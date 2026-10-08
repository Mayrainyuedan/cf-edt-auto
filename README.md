# CF-EDT Cloud Auto

每天自动筛选真正适合你这套 EDT 节点的 Cloudflare 优选 IP，并把结果写回 EdgeTunnel 的 `ADD.txt`。

你的节点固定参数：

- SNI / Host：`bxian1.pages.dev`
- WS Path：`/proxyip=proxy.mia.xx.kg`
- VLESS UUID：通过 GitHub Actions Secret `VLESS_UUID` 提供
- 默认端口：443
- 默认地区：NRT
- 默认阈值：延迟 < 200 ms、实际 VLESS+WS 下载 > 20 MB/s
- 默认保留：10 个

与普通 CFData-WEB TCPing 不同，这个版本先用 `CF-RAY` 判断实际 Cloudflare Colo，再用真实的 **TLS + WebSocket + VLESS** 链路测速。只有完整链路通过才进入最终名单。

## 免费云端运行

GitHub 的公共仓库使用标准 GitHub-hosted runner 是免费的；GitHub 官方文档目前明确说明 public repository 的 standard runner 不计费。\n
因此把这个目录放进一个 public repository，然后启用 Actions 即可每天自动运行。

## 需要设置的 GitHub Secrets

`EDT_URL`

例如：`https://你的EDT域名`

`EDT_PASSWORD`

你的 EDT 管理面板密码。

`VLESS_UUID`

你当前节点使用的 VLESS UUID。

不要把密码或 UUID 写进仓库文件。

## 使用方式

1. 新建一个 **Public GitHub repository**。
2. 把本目录中的文件上传进去。
3. 在 Settings → Secrets and variables → Actions 中添加上面的 3 个 Secrets。
4. 打开 Actions，运行 `CF-EDT daily optimizer` 一次做首次测试。
5. 测试成功以后，工作流每天自动运行。

默认时间是北京时间每天 03:30，对应 GitHub Actions cron 的 `19:30 UTC`。

## 筛选流程

Cloudflare 官方 IPv4 网段 → 每天轮换抽样 → TLS/HTTP 探测 `bxian1.pages.dev` → 从 `CF-RAY` 提取 Colo → 只保留 NRT → 真实 WS 升级 → VLESS 首包校验 → 经你的 `/proxyip=proxy.mia.xx.kg` 发起实际下载 → 按速度优先、延迟次优排序 → 写入 EDT `ADD.txt`。

如果达标数量少于 3 个，脚本不会覆盖远端 `ADD.txt`，避免一次网络波动导致整个订阅被清空。
