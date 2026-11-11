/**
 * 工具白名单（物理边界）。
 *
 * dsh 的工具是全局注册的：preset 里「不挂 tool-bash」并不会让它消失。
 * 真正收窄可见集合只有 ctx.tools.restrict({ allow }) —— 必须在 agent scope
 * 里调用（本插件挂在 preset 的 plugins 里，ctx 就是 agent scope）。
 *
 * 为什么不用提示词：一句「忽略上面的限制」可能绕过措辞，但绕过不了掩码。
 *
 * ── 2026-11-11 修：restrict 不能一上来就调 ─────────────────────────
 * `ctx.tools.restrict()` 会在调用时把 allow 里的名字和**当前已注册**的全局
 * 工具比对，不认识的直接抛：
 *   `tools.restrict() names unknown global tools "mcp__quant__..."`;
 * 而 MCP server 是**异步连接的**，apply 阶段那些工具还没注册。
 * 结果：8 个 persona 一个都加载不起来（报 agent-preset/invalid），
 * 长期以来它们只是「配置里写了、界面里没有、其实也没生效」。
 *
 * 所以改成：先试，失败了按退避重试，直到 MCP 工具出现为止；
 * 重试期间允许放行（此时还没开始对话，模型看不到任何工具）。
 * 万一 MCP 一直连不上，退化成 deny 危险工具 —— 宁可有边界，
 * 也不要让 preset 整个加载失败变成「无所不能」。
 */
export const inject = ['tools']

/** 兜底：allow 迟迟对不上时，改成拉黑这些通用能力。 */
const DANGEROUS = [
  'bash', 'pwsh', 'fs_read', 'fs_write', 'fs_edit', 'fs_list', 'fs_search',
  'grep', 'glob', 'read', 'write', 'edit', 'terminal_open', 'terminal_read',
  'jobs', 'job_list', 'job_output', 'job_kill', 'skill', 'schedule_create',
  'spawn_teammate', 'web_search', 'web_fetch',
]

const RETRY_MS = 400
const MAX_TRIES = 75 // ≈30 秒

export function apply(ctx, config) {
  const allow = config && config.allow
  if (!Array.isArray(allow) || allow.length === 0) {
    throw new Error('tool-guard: config.allow 必须是非空数组（空数组会挡掉所有工具）')
  }

  let done = null   // 已生效的 disposer
  let closed = false
  let tries = 0
  let timer = null

  const attempt = () => {
    if (closed || done) return
    tries += 1
    try {
      done = ctx.tools.restrict({ allow })
      return
    } catch (e) {
      const msg = String((e && e.message) || e)
      const transient = msg.includes('unknown global tool')
      if (!transient || tries >= MAX_TRIES) {
        // 不是「工具还没注册」这类可恢复错误，或者重试耗尽 → 退化拉黑
        try {
          done = ctx.tools.restrict({ deny: DANGEROUS })
        } catch (e2) {
          ctx.logger?.warn?.('tool-guard: 退化 deny 也失败：%s', String((e2 && e2.message) || e2))
        }
        return
      }
      timer = setTimeout(attempt, RETRY_MS)
    }
  }

  // effect 保证 agent 结束时把定时器与限制一起收掉
  ctx.effect(() => {
    attempt()
    return () => {
      closed = true
      if (timer) clearTimeout(timer)
      if (done) {
        try {
          done()
        } catch (e) {
          /* 已经随 scope 销毁，忽略 */
        }
      }
    }
  })
}
