# CF-EDT Cloud Auto v1.1

这个版本修正了上一版两个问题：

1. GitHub Actions 运行器不在用户本地网络，因此不能用固定 `NRT` 过滤器模拟用户本地线路。默认 `colo` 现在为空，先筛选可达且低延迟的 Cloudflare IP，再做真实 VLESS+WS+TLS 测速。
2. 当合格 IP 少于安全阈值时，脚本会直接保留 EDT 原有 ADD.txt，不再无意义地登录 EDT。

注意：GitHub-hosted runner 测到的是 GitHub runner 的网络，不等于你的电信/联通/移动本地线路。要做到“每天自动按你本地网络优选”，仍然需要一个位于你的实际运营商网络附近的测速端，或者由你本地客户端负责最终 url-test。

Secrets：
- EDT_URL：例如 `https://你的EDT域名`，不要填 `/admin`
- EDT_PASSWORD：EDT 管理密码
- VLESS_UUID：你的 VLESS UUID

默认阈值：延迟 < 200ms、真实 VLESS+WS 下载 > 20MB/s，至少 3 个才更新远端 ADD.txt。
