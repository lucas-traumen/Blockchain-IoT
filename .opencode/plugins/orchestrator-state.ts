import { randomUUID } from "node:crypto"
import { mkdir, readFile, readdir, rename, unlink, writeFile } from "node:fs/promises"
import { join } from "node:path"
import { tool, type Plugin } from "@opencode-ai/plugin"

type Phase = "pending" | "running" | "blocked" | "done" | "cancelled"
type TaskStatus = "pending" | "dispatching" | "running" | "returned" | "done" | "error" | "blocked" | "cancelled"

type HistoryEntry = { session_id: string | null; call_id: string | null; status: TaskStatus; at: string }

type TaskState = {
  session_id: string | null
  call_id: string | null
  status: TaskStatus
  attempt: number
  agent: string
  error: string | null
  result: string | null
  evidence: string | null
  updated_at: string | null
  history: HistoryEntry[]
}

type RunState = {
  schema_version: number
  run_id: string
  project_id: string
  worktree: string
  main_session_id: string | null
  main_history: Array<{ session_id: string; at: string }>
  phase: Phase
  next_action: string | null
  updated_at: string | null
  tasks: Record<string, TaskState>
}

type RunFile = { file: string; state: RunState }

type PluginCtx = {
  stateDir: string
  projectId: string
  worktree: string
  client: any
  log: (level: "debug" | "info" | "warn" | "error", message: string, extra?: Record<string, unknown>) => Promise<void>
}

const BOOT = Date.now()
const FRESH_MS = 30_000
const TEXT_LIMIT = 1500
const TASK_TOOL = "task"
const ORCHESTRATOR = "orchestrator"
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const MARKER_RE = /^\[([^\]/]+)\/([^\]]+)\]\s*/
const KEY_RE = /^([A-Za-z0-9][A-Za-z0-9._-]*):([A-Za-z0-9][A-Za-z0-9._-]*)/

function now(): string {
  return new Date().toISOString()
}

function truncate(value: unknown, limit = TEXT_LIMIT): string {
  const text = String(value ?? "")
  return text.length > limit ? text.slice(0, limit) + "…" : text
}

function isFresh(iso: string | null | undefined): boolean {
  const time = Date.parse(iso ?? "")
  return Number.isFinite(time) && Date.now() - time < FRESH_MS
}

function fromThisInstance(iso: string | null | undefined): boolean {
  const time = Date.parse(iso ?? "")
  return Number.isFinite(time) && time >= BOOT
}

function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

function parseDescription(description: string): { taskKey: string | null; clean: string } {
  let text = String(description ?? "").trim()
  const marker = text.match(MARKER_RE)
  if (marker && UUID_RE.test(marker[1])) text = text.slice(marker[0].length)
  const key = text.match(KEY_RE)
  return { taskKey: key ? `${key[1]}:${key[2]}` : null, clean: text }
}

function childIdFrom(metadata: any): string | null {
  const id = metadata?.sessionId ?? metadata?.sessionID
  return typeof id === "string" && id.length > 0 ? id : null
}

const gates = new Map<string, Promise<void>>()

async function withRunLock<T>(lockPath: string, fn: () => Promise<T>): Promise<T> {
  const previous = gates.get(lockPath) ?? Promise.resolve()
  let openGate = () => {}
  const gate = new Promise<void>((resolve) => {
    openGate = resolve
  })
  gates.set(lockPath, previous.then(() => gate))
  await previous
  try {
    await acquireLock(lockPath)
    try {
      return await fn()
    } finally {
      await unlink(lockPath).catch(() => {})
    }
  } finally {
    openGate()
  }
}

async function acquireLock(lockPath: string): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      await writeFile(lockPath, JSON.stringify({ pid: process.pid, at: now() }), { flag: "wx" })
      return
    } catch (error: any) {
      if (error?.code !== "EEXIST") throw error
      let holder: any = null
      try {
        holder = JSON.parse(await readFile(lockPath, "utf8"))
      } catch {}
      const foreign = typeof holder?.pid === "number" && holder.pid !== process.pid
      if (foreign && pidAlive(holder.pid) && isFresh(holder?.at)) {
        throw new Error(`run state đang bị khóa bởi pid ${holder.pid} (process khác đang ghi)`)
      }
      await unlink(lockPath).catch(() => {})
    }
  }
  throw new Error(`không lấy được lock: ${lockPath}`)
}

async function readState(file: string): Promise<RunState | null> {
  try {
    const parsed = JSON.parse(await readFile(file, "utf8"))
    if (parsed && typeof parsed === "object" && parsed.schema_version === 3 && typeof parsed.run_id === "string") {
      parsed.tasks ??= {}
      parsed.main_history ??= []
      return parsed as RunState
    }
    return null
  } catch {
    return null
  }
}

async function writeState(file: string, state: RunState): Promise<void> {
  state.updated_at = now()
  const tmp = `${file}.tmp`
  await writeFile(tmp, JSON.stringify(state, null, 2) + "\n")
  await rename(tmp, file)
}

async function listRuns(ctx: PluginCtx): Promise<RunFile[]> {
  const runs: RunFile[] = []
  let names: string[] = []
  try {
    names = await readdir(ctx.stateDir)
  } catch {
    return runs
  }
  for (const name of names) {
    if (!name.endsWith(".json")) continue
    const file = join(ctx.stateDir, name)
    const state = await readState(file)
    if (state) runs.push({ file, state })
  }
  return runs
}

async function findRunByMain(ctx: PluginCtx, sessionID: string): Promise<RunFile | null> {
  for (const run of await listRuns(ctx)) {
    if (run.state.main_session_id === sessionID && run.state.worktree === ctx.worktree && run.state.project_id === ctx.projectId) {
      return run
    }
  }
  return null
}

async function commit(ctx: PluginCtx, runId: string, mutate: (state: RunState) => void | Promise<void>): Promise<RunState> {
  const file = join(ctx.stateDir, `${runId}.json`)
  const lock = join(ctx.stateDir, `${runId}.lock`)
  return withRunLock(lock, async () => {
    const state = await readState(file)
    if (!state) throw new Error(`run state không tồn tại: ${runId}`)
    await mutate(state)
    await writeState(file, state)
    return state
  })
}

async function createRun(ctx: PluginCtx, mainSessionId: string): Promise<string> {
  const runId = randomUUID()
  const state: RunState = {
    schema_version: 3,
    run_id: runId,
    project_id: ctx.projectId,
    worktree: ctx.worktree,
    main_session_id: mainSessionId,
    main_history: [],
    phase: "pending",
    next_action: "Duyệt PLAN.md trước khi triển khai",
    updated_at: null,
    tasks: {},
  }
  await writeState(join(ctx.stateDir, `${runId}.json`), state)
  return runId
}

function findTaskByCall(state: RunState, callID: string): [string, TaskState] | null {
  for (const entry of Object.entries(state.tasks)) {
    if (entry[1].call_id === callID) return entry
  }
  return null
}

function findTaskBySession(state: RunState, sessionID: string): [string, TaskState] | null {
  for (const entry of Object.entries(state.tasks)) {
    if (entry[1].session_id === sessionID) return entry
  }
  return null
}

function summarize(state: RunState) {
  return {
    run_id: state.run_id,
    phase: state.phase,
    main_session_id: state.main_session_id,
    main_history: state.main_history,
    next_action: state.next_action,
    updated_at: state.updated_at,
    tasks: Object.fromEntries(
      Object.entries(state.tasks).map(([key, task]) => [
        key,
        {
          status: task.status,
          attempt: task.attempt,
          session_id: task.session_id,
          call_id: task.call_id,
          agent: task.agent,
          error: task.error,
          evidence: task.evidence,
        },
      ]),
    ),
  }
}

async function beforeTaskDispatch(
  ctx: PluginCtx,
  input: { sessionID: string; callID: string },
  output: { args: any },
): Promise<void> {
  const args = output.args
  if (!args || typeof args !== "object") return
  const description = String(args.description ?? "")
  const subagent = String(args.subagent_type ?? "")
  const { taskKey, clean } = parseDescription(description)

  const run = await findRunByMain(ctx, input.sessionID)

  if (!taskKey || !subagent || taskKey.split(":")[1] !== subagent) {
    if (!run) return
    throw new Error(
      `[orchestrator-state] description của Task phải bắt đầu bằng task_key "<việc>:<agent>" (ví dụ "M1:coder") và phần agent phải khớp subagent_type. Nhận được: "${truncate(description, 120)}"`,
    )
  }

  if (args.task_id) {
    if (!run) return
    const taskId = String(args.task_id)
    const child = await ctx.client.session
      .get({ path: { id: taskId } })
      .then((result: any) => result?.data)
      .catch(() => null)
    if (!child || child.parentID !== run.state.main_session_id || child.projectID !== ctx.projectId) {
      throw new Error(
        `[orchestrator-state] session ${taskId} không tồn tại hoặc không phải child của main ${run.state.main_session_id} trong project này; không tái sử dụng task_id.`,
      )
    }
    let key = taskKey
    let task = run.state.tasks[key]
    if (!task || task.session_id !== taskId) {
      const bySession = findTaskBySession(run.state, taskId)
      if (!bySession) {
        throw new Error(`[orchestrator-state] session ${taskId} không thuộc task nào của run ${run.state.run_id}; dispatch mới thay vì resume.`)
      }
      key = bySession[0]
      task = bySession[1]
    }
    if (task.status === "dispatching" && task.call_id !== input.callID && fromThisInstance(task.updated_at)) {
      throw new Error(`[orchestrator-state] task ${key} đang được dispatch (call ${task.call_id}); không dispatch trùng.`)
    }
    await commit(ctx, run.state.run_id, (state) => {
      const target = state.tasks[key]
      if (!target) throw new Error(`[orchestrator-state] task không còn tồn tại: ${key}`)
      if (target.session_id && target.session_id !== taskId) {
        target.history.push({ session_id: target.session_id, call_id: target.call_id, status: target.status, at: now() })
        target.session_id = taskId
      }
      target.status = "dispatching"
      target.call_id = input.callID
      target.error = null
      target.updated_at = now()
      if (state.phase === "pending") state.phase = "running"
      state.next_action = `Chờ kết quả ${key}`
    })
    output.args.description = `[${run.state.run_id}/${key}] ${clean}`
    return
  }

  let runId = run?.state.run_id
  if (!runId) runId = await createRun(ctx, input.sessionID)

  const previous = run?.state.tasks[taskKey]
  if (previous && (previous.status === "dispatching" || previous.status === "running") && previous.call_id !== input.callID && fromThisInstance(previous.updated_at)) {
    throw new Error(
      `[orchestrator-state] task ${taskKey} đang chạy (call ${previous.call_id}); không dispatch trùng. Dùng task_id của session cũ để resume hoặc chờ kết quả.`,
    )
  }

  await commit(ctx, runId, (state) => {
    const existing = state.tasks[taskKey]
    const history = existing?.history ?? []
    if (existing?.session_id) {
      history.push({ session_id: existing.session_id, call_id: existing.call_id, status: existing.status, at: now() })
    }
    state.tasks[taskKey] = {
      session_id: null,
      call_id: input.callID,
      status: "dispatching",
      attempt: (existing?.attempt ?? 0) + 1,
      agent: subagent,
      error: null,
      result: null,
      evidence: null,
      updated_at: now(),
      history,
    }
    if (state.phase === "pending") state.phase = "running"
    state.next_action = `Chờ kết quả ${taskKey}`
  })
  output.args.description = `[${runId}/${taskKey}] ${clean}`
}

async function afterTaskDispatch(
  ctx: PluginCtx,
  input: { sessionID: string; callID: string; args: any },
  output: { title?: string; output?: string; metadata?: any },
): Promise<void> {
  const run = await findRunByMain(ctx, input.sessionID)
  if (!run) return
  const child = childIdFrom(output?.metadata)
  const text = String(output?.output ?? "")
  const entry = findTaskByCall(run.state, input.callID) ?? (child ? findTaskBySession(run.state, child) : null)
  if (!entry) return
  const key = entry[0]
  const failed = text.includes('state="error"') || text.includes("<task_error>")
  await commit(ctx, run.state.run_id, (state) => {
    const task = state.tasks[key]
    if (!task) return
    if (child && !task.session_id) task.session_id = child
    if (task.status !== "done") {
      if (failed) {
        task.status = "error"
        task.error = truncate(text)
      } else {
        task.status = "returned"
        task.result = truncate(text)
      }
    }
    task.updated_at = now()
  })
}

async function onSessionCreated(ctx: PluginCtx, info: any): Promise<void> {
  if (!info?.id || !info.parentID) return
  const run = await findRunByMain(ctx, info.parentID)
  if (!run) return
  const pending = Object.entries(run.state.tasks).filter(
    ([, task]) => task.status === "dispatching" && !task.session_id && isFresh(task.updated_at),
  )
  if (pending.length !== 1) return
  const key = pending[0][0]
  await commit(ctx, run.state.run_id, (state) => {
    const task = state.tasks[key]
    if (task && task.status === "dispatching" && !task.session_id) {
      task.session_id = String(info.id)
      task.status = "running"
      task.updated_at = now()
    }
  })
}

async function onPartUpdated(ctx: PluginCtx, part: any): Promise<void> {
  if (!part || part.type !== "tool" || part.tool !== TASK_TOOL || !part.sessionID) return
  const run = await findRunByMain(ctx, part.sessionID)
  if (!run) return
  const state = part.state ?? {}
  const metadata = state.metadata ?? part.metadata ?? {}
  const child = childIdFrom(metadata)
  const parent = metadata?.parentSessionId ?? metadata?.parentSessionID
  const validChild = child && (!parent || parent === part.sessionID) ? child : null
  const entry = findTaskByCall(run.state, String(part.callID ?? ""))
  const key = entry?.[0] ?? (validChild ? findTaskBySession(run.state, validChild)?.[0] : undefined)
  if (!key) return
  const status = state.status
  await commit(ctx, run.state.run_id, (state) => {
    const task = state.tasks[key]
    if (!task || task.status === "done") return
    if (validChild && !task.session_id) task.session_id = validChild
    if (status === "running" && task.status === "dispatching") {
      task.status = "running"
    } else if (status === "completed") {
      task.status = "returned"
      task.result ??= truncate(state.output)
    } else if (status === "error") {
      task.status = "error"
      task.error = truncate(state.error)
    }
    task.updated_at = now()
  })
}

async function onSessionIdle(ctx: PluginCtx, sessionID: string): Promise<void> {
  if (!sessionID) return
  for (const { state } of await listRuns(ctx)) {
    const keys = Object.entries(state.tasks)
      .filter(([, task]) => task.session_id === sessionID && task.status === "running")
      .map(([key]) => key)
    if (!keys.length) continue
    await commit(ctx, state.run_id, (current) => {
      for (const key of keys) {
        const task = current.tasks[key]
        if (task && task.status === "running") {
          task.status = "returned"
          task.updated_at = now()
        }
      }
    })
  }
}

async function onSessionError(ctx: PluginCtx, properties: any): Promise<void> {
  const sessionID = properties?.sessionID
  if (!sessionID) return
  const message = properties?.error?.data?.message ?? properties?.error?.message ?? properties?.error?.name ?? "unknown error"
  for (const { state } of await listRuns(ctx)) {
    const keys = Object.entries(state.tasks)
      .filter(([, task]) => task.session_id === sessionID && task.status !== "done" && task.status !== "error")
      .map(([key]) => key)
    if (!keys.length) continue
    await commit(ctx, state.run_id, (current) => {
      for (const key of keys) {
        const task = current.tasks[key]
        if (task && task.status !== "done" && task.status !== "error") {
          task.status = "error"
          task.error = truncate(message)
          task.updated_at = now()
        }
      }
    })
  }
}

async function onSessionDeleted(ctx: PluginCtx, info: any): Promise<void> {
  if (!info?.id) return
  if (info.parentID) {
    const run = await findRunByMain(ctx, info.parentID)
    if (!run) return
    const entry = findTaskBySession(run.state, info.id)
    if (!entry) return
    const key = entry[0]
    await commit(ctx, run.state.run_id, (state) => {
      const task = state.tasks[key]
      if (task && task.session_id === info.id) {
        task.history.push({ session_id: task.session_id, call_id: task.call_id, status: task.status, at: now() })
        task.session_id = null
        task.status = "blocked"
        task.error = "session bị xóa; xác minh phần đã làm bằng git diff trước khi dispatch lại"
        task.updated_at = now()
      }
    })
    return
  }
  for (const { state } of await listRuns(ctx)) {
    if (state.main_session_id === info.id) {
      await commit(ctx, state.run_id, (current) => {
        current.phase = "cancelled"
        current.next_action = "Main session bị xóa"
      })
    }
  }
}

async function reconcile(ctx: PluginCtx): Promise<void> {
  for (const { state } of await listRuns(ctx)) {
    try {
      await commit(ctx, state.run_id, async (current) => {
        let main: any = null
        if (current.main_session_id) {
          main = await ctx.client.session
            .get({ path: { id: current.main_session_id } })
            .then((result: any) => result?.data)
            .catch(() => null)
          if (!main) {
            current.phase = "blocked"
            current.next_action = `Main session ${current.main_session_id} không tồn tại; handoff bằng checkpoint adopt hoặc tạo run mới.`
          }
        }
        if (main) {
          const messages =
            (await ctx.client.session
              .messages({ path: { id: main.id } })
              .then((result: any) => result?.data)
              .catch(() => null)) ?? []
          for (const message of messages) {
            for (const part of message?.parts ?? []) {
              if (!part || part.type !== "tool" || part.tool !== TASK_TOOL) continue
              const key = findTaskByCall(current, String(part.callID ?? ""))?.[0]
              if (!key) continue
              const task = current.tasks[key]
              const child = childIdFrom(part.state?.metadata ?? part.metadata)
              if (child && !task.session_id) task.session_id = child
              const partStatus = part.state?.status
              if (partStatus === "completed" && task.status !== "done" && task.status !== "returned") {
                task.status = "returned"
                task.result ??= truncate(part.state?.output)
                task.updated_at = now()
              } else if (partStatus === "error" && task.status !== "done") {
                task.status = "error"
                task.error = truncate(part.state?.error)
                task.updated_at = now()
              }
            }
          }
          const children =
            (await ctx.client.session
              .children({ path: { id: main.id } })
              .then((result: any) => result?.data)
              .catch(() => null)) ?? []
          const assigned = new Set(
            Object.values(current.tasks)
              .map((task) => task.session_id)
              .filter((id): id is string => Boolean(id)),
          )
          for (const key of Object.keys(current.tasks)) {
            const task = current.tasks[key]
            if (task.session_id || (task.status !== "dispatching" && task.status !== "running")) continue
            const marker = `[${current.run_id}/${key}]`
            const matches = children.filter((child: any) => !assigned.has(child.id) && String(child.title ?? "").includes(marker))
            if (matches.length === 1) {
              task.session_id = matches[0].id
              task.status = "running"
              task.updated_at = now()
              assigned.add(matches[0].id)
            } else if (matches.length > 1) {
              task.status = "blocked"
              task.error = "nhiều child session khớp marker; cần đối soát tay"
              task.updated_at = now()
            }
          }
        }
        for (const key of Object.keys(current.tasks)) {
          const task = current.tasks[key]
          if (task.status !== "dispatching" && task.status !== "running") continue
          if (isFresh(task.updated_at)) continue
          if (task.session_id) {
            const child = await ctx.client.session
              .get({ path: { id: task.session_id } })
              .then((result: any) => result?.data)
              .catch(() => null)
            if (!child) {
              task.history.push({ session_id: task.session_id, call_id: task.call_id, status: task.status, at: now() })
              task.session_id = null
              task.status = "blocked"
              task.error = "session mất sau gián đoạn; xác minh phần đã làm bằng git diff trước khi dispatch lại"
              task.updated_at = now()
            }
          } else {
            task.status = "pending"
            task.error = "dispatch bị gián đoạn trước khi tạo session"
            task.updated_at = now()
          }
        }
      })
    } catch (error) {
      await ctx.log("error", "reconcile failed", { run_id: state.run_id, error: String(error) })
    }
  }
}

async function runCheckpoint(ctx: PluginCtx, args: any, context: { sessionID: string; agent: string }): Promise<string> {
  const run = await findRunByMain(ctx, context.sessionID)

  if (args.action === "status") {
    if (run) return JSON.stringify(summarize(run.state), null, 2)
    const runs = await listRuns(ctx)
    if (!runs.length) {
      return 'Chưa có run nào trong .opencode/state/. Dispatch task đầu tiên (description bắt đầu bằng task_key như "M1:coder") để tạo run.'
    }
    return [
      "Không có run nào gắn với session này. Các run hiện có:",
      ...runs.map(({ state }) => `- run_id=${state.run_id} phase=${state.phase} main=${state.main_session_id} next=${state.next_action ?? "-"}`),
      'Dùng action "adopt" kèm run_id để nhận run cũ, hoặc dispatch task mới để tạo run khác.',
    ].join("\n")
  }

  if (args.action === "adopt") {
    const runId = String(args.run_id ?? "")
    if (!UUID_RE.test(runId)) throw new Error("adopt cần run_id hợp lệ (UUID)")
    const state = await commit(ctx, runId, (current) => {
      if (current.main_session_id === context.sessionID) return
      if (current.main_session_id) current.main_history.push({ session_id: current.main_session_id, at: now() })
      current.main_session_id = context.sessionID
      current.next_action = "Tiếp tục sau handoff main"
      if (current.phase === "blocked" || current.phase === "cancelled") current.phase = "running"
    })
    return JSON.stringify(summarize(state), null, 2)
  }

  if (context.agent !== ORCHESTRATOR) throw new Error("checkpoint chỉ dành cho orchestrator")

  if (!run) throw new Error('Không có run nào cho session này; gọi action "status" để xem các run hiện có')

  if (args.action === "done") {
    const key = String(args.task_key ?? "")
    if (!key) throw new Error("done cần task_key")
    const state = await commit(ctx, run.state.run_id, (current) => {
      const task = current.tasks[key]
      if (!task) throw new Error(`task không tồn tại: ${key}`)
      if (task.status === "dispatching" || task.status === "running") {
        throw new Error(`task ${key} đang chạy; chờ kết quả trước khi chốt done`)
      }
      task.status = "done"
      if (args.evidence) task.evidence = truncate(args.evidence, 2000)
      task.error = null
      task.updated_at = now()
      const keys = Object.keys(current.tasks)
      if (keys.length > 0 && keys.every((other) => current.tasks[other].status === "done")) {
        current.phase = "done"
        current.next_action = "Workflow hoàn tất"
      }
    })
    return JSON.stringify(summarize(state), null, 2)
  }

  if (args.action === "blocked") {
    const key = args.task_key ? String(args.task_key) : null
    const state = await commit(ctx, run.state.run_id, (current) => {
      if (key) {
        const task = current.tasks[key]
        if (!task) throw new Error(`task không tồn tại: ${key}`)
        task.status = "blocked"
        if (args.reason) task.error = truncate(args.reason)
        task.updated_at = now()
      } else {
        current.phase = "blocked"
        if (args.reason) current.next_action = truncate(args.reason, 500)
      }
    })
    return JSON.stringify(summarize(state), null, 2)
  }

  if (args.action === "next") {
    const next = String(args.next_action ?? "")
    if (!next) throw new Error("next cần next_action")
    await commit(ctx, run.state.run_id, (current) => {
      current.next_action = truncate(next, 500)
    })
    return `next_action: ${truncate(next, 500)}`
  }

  if (args.action === "phase") {
    const phase = args.phase as Phase | undefined
    if (!phase) throw new Error("phase cần giá trị phase")
    const state = await commit(ctx, run.state.run_id, (current) => {
      current.phase = phase
      if (args.next_action) current.next_action = truncate(String(args.next_action), 500)
    })
    return JSON.stringify(summarize(state), null, 2)
  }

  throw new Error(`action không hỗ trợ: ${args.action}`)
}

export const OrchestratorState: Plugin = async (input) => {
  const worktree = input.worktree
  const stateDir = join(worktree, ".opencode", "state")
  const ctx: PluginCtx = {
    stateDir,
    projectId: input.project?.id ?? "",
    worktree,
    client: input.client,
    log: async (level, message, extra) => {
      try {
        await input.client.app.log({ body: { service: "orchestrator-state", level, message, extra } })
      } catch {}
    },
  }
  await mkdir(stateDir, { recursive: true }).catch(() => {})
  void reconcile(ctx).catch(async (error) => {
    await ctx.log("error", "reconcile error", { error: String(error) })
  })

  return {
    "tool.execute.before": async (hookInput, hookOutput) => {
      if (hookInput.tool !== TASK_TOOL) return
      await beforeTaskDispatch(ctx, hookInput, hookOutput)
    },
    "tool.execute.after": async (hookInput, hookOutput) => {
      if (hookInput.tool !== TASK_TOOL) return
      await afterTaskDispatch(ctx, hookInput, hookOutput)
    },
    event: async ({ event }) => {
      try {
        if (event.type === "session.created") {
          await onSessionCreated(ctx, (event as any).properties?.info)
        } else if (event.type === "message.part.updated") {
          await onPartUpdated(ctx, (event as any).properties?.part)
        } else if (event.type === "session.idle") {
          await onSessionIdle(ctx, (event as any).properties?.sessionID)
        } else if (event.type === "session.error") {
          await onSessionError(ctx, (event as any).properties)
        } else if (event.type === "session.deleted") {
          await onSessionDeleted(ctx, (event as any).properties?.info)
        }
      } catch (error) {
        await ctx.log("error", "event handler error", { type: (event as any).type, error: String(error) })
      }
    },
    tool: {
      checkpoint: tool({
        description:
          "Quản lý checkpoint workflow của orchestrator: status (xem trạng thái run), done (chốt task sau xác minh, kèm evidence), blocked (đánh dấu task/run bị chặn, kèm reason), next (cập nhật next_action), phase (đặt phase run), adopt (nhận run cũ khi handoff sang main mới, kèm run_id).",
        args: {
          action: tool.schema.enum(["status", "done", "blocked", "next", "phase", "adopt"]),
          task_key: tool.schema.string().optional(),
          reason: tool.schema.string().optional(),
          evidence: tool.schema.string().optional(),
          next_action: tool.schema.string().optional(),
          phase: tool.schema.enum(["pending", "running", "blocked", "done", "cancelled"]).optional(),
          run_id: tool.schema.string().optional(),
        },
        async execute(args, context) {
          return runCheckpoint(ctx, args, context)
        },
      }),
    },
  }
}
