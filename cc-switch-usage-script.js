// cc-switch 用量查询自定义脚本 —— cx2cc / codex-bridge (ChatGPT Codex 订阅)
//
// 用法：cc-switch 供应商卡片 -> 用量查询(📊) -> 启用 -> 模板选「自定义」，
// 把整段脚本粘贴进去。{{baseUrl}} / {{apiKey}} 由 cc-switch 从供应商配置注入，
// 所以同一份脚本在每台机器都不用改：
//   vircs / mac173 等 tailnet 机器: baseUrl = http://<cx2cc-host>:8901
//   txfa608 (不在 tailnet): baseUrl = http://127.0.0.1:18901，走既有隧道回 vircs
// 注意：cc-switch 校验「非 localhost 必须 HTTPS」（新版仅自定义模板豁免）。
// 若被拦，把下面 url 换成 HTTPS 入口 "https://<host>.<tailnet>.ts.net/usage"
// （tailscale serve 反代，tailnet 内自动证书；mac173 上的副本已是这个形态），
// 此时配置面板 Base URL 留空、只填 API Key。localhost 隧道入口不受此限。
// 注意 2：旧版 cc-switch 的自定义模板不注入 {{apiKey}}/{{baseUrl}}（上游 PR #1479
// 才修），症状是测试返回 cx2cc 的 502 "No upstream API key configured"。
// 遇到就把占位符直接替换成字面量（mac173/txfa608 上的副本已是写死形态）。
// apiKey = 共享密钥（供应商配置里的 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY）。
// 查询始终由 vircs 上的 cx2cc -> codex-bridge 代查 ChatGPT 后端后传回；
// 客户端机器不直连 chatgpt.com，也不需要本地 Codex 凭证。
//
// 返回的数据来自 chatgpt.com/backend-api/wham/usage（经 cx2cc -> codex-bridge 透传），
// 是订阅限额窗口的已用百分比，不是美元余额。prolite 计划只有一个 7 天窗口；
// 其他计划可能是 5h(primary) + 周(secondary) 双窗口，脚本按窗口时长动态标注。
({
  request: {
    url: "{{baseUrl}}/usage",
    method: "GET",
    headers: {
      "x-api-key": "{{apiKey}}",
      "User-Agent": "cc-switch/1.0"
    }
  },
  extractor: function (response) {
    var rl = response.rate_limit || {};
    var windows = [];
    if (rl.primary_window) windows.push(rl.primary_window);
    if (rl.secondary_window) windows.push(rl.secondary_window);
    if (windows.length === 0) {
      return {
        isValid: false,
        invalidMessage: (response.error && response.error.message) || "响应中没有 rate_limit 窗口"
      };
    }
    function label(w) {
      var h = Math.round(w.limit_window_seconds / 3600);
      return h >= 24 ? Math.round(h / 24) + "d" : h + "h";
    }
    function resetText(w) {
      var d = new Date(w.reset_at * 1000);
      var p = function (n) { return (n < 10 ? "0" : "") + n; };
      return (d.getMonth() + 1) + "/" + d.getDate() + " " + p(d.getHours()) + ":" + p(d.getMinutes());
    }
    // 用得最满的窗口是实际约束，作为主进度条展示。
    var main = windows[0];
    for (var i = 1; i < windows.length; i++) {
      if (windows[i].used_percent > main.used_percent) main = windows[i];
    }
    var parts = [];
    for (var j = 0; j < windows.length; j++) {
      var w = windows[j];
      parts.push(label(w) + "窗口已用 " + w.used_percent + "%，" + resetText(w) + " 重置");
    }
    // vircs 上现在挂着多个 ChatGPT 订阅，/usage 报的是「当前正在服务的那个」，
    // 所以把账号标识带进 planName，切换发生时这里会跟着变。
    var who = "";
    if (response.email) {
      who = " · " + String(response.email).split("@")[0].slice(0, 8);
    }
    return {
      isValid: rl.allowed !== false,
      invalidMessage: rl.limit_reached ? "已达速率限制" : "",
      planName: "ChatGPT " + (response.plan_type || "?") + who,
      used: main.used_percent,
      total: 100,
      remaining: 100 - main.used_percent,
      unit: "%",
      extra: parts.join(" | ")
    };
  }
})
