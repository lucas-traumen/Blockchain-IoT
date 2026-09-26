# AGENTS.md

Luật vận hành cho mọi agent làm việc trong repo này (Blockchain-IoT). Agent mặc định là `orchestrator`.

## Vai trò và model

| Agent        | Model                                        | Nhiệm vụ                       | Giới hạn chính                          |
| ------------ | -------------------------------------------- | ------------------------------ | --------------------------------------- |
| orchestrator | `xkiro/openai/gpt-5.6-sol` (fallback Opus)   | Lập plan, giao việc, tổng hợp  | Chờ duyệt plan; không dispatch trùng    |
| coder        | `xkiro/z-ai/glm-5.3-flash`                   | Sửa code trong phạm vi giao    | Không spawn worker                      |
| reviewer     | `xkiro/openai/gpt-5.6-terra`                 | Review, không sửa file         | `edit`/`bash`: deny                     |
| tester       | `xkiro/openai/gpt-5.6-terra`                 | Chạy test/build, không sửa source | `edit`: deny; `bash`: ask            |

Reasoning: dùng mặc định provider — sol/terra/opus đã có `reasoningEffort: max` trong config global; glm-5.3-flash chưa xác minh effort ladder nên **không đặt** reasoning cho model này. Khi đổi model, kiểm tra lại bằng `opencode models`.

Fallback model của orchestrator: `xkiro/anthropic/claude-opus-5`.

## File chuẩn

- `PLAN.md` — plan hiện tại (mốc, task, tiêu chí). Orchestrator tạo/cập nhật, được commit.
- `PROJECT_MEMORY.md` — quyết định bền vững (kiến trúc, quy ước). Được commit.
- `.opencode/state/<run_id>.json` — checkpoint runtime của plugin `orchestrator-state`. **KHÔNG commit** (đã có trong `.gitignore`). ID trong state là con trỏ đến dữ liệu phiên OpenCode, không thay thế plan/memory; copy file sang máy khác không mang theo hội thoại.

Cấu hình: `opencode.json` (default_agent + permission), agent ở `.opencode/agents/`, plugin ở `.opencode/plugins/orchestrator-state.ts`. **Không sửa cấu hình global `~/.config/opencode/`** — project-local không cô lập hoàn toàn: OpenCode merge config global và lưu dữ liệu phiên ngoài repo.

## Luật điều phối

1. Đầu phiên: đọc plan, memory, checkpoint (tool `checkpoint`, action `status`) và `git status`/`git diff`; chờ người dùng duyệt plan trước khi triển khai.
2. Mỗi việc có task_key dạng `<mã>:<agent>` (ví dụ `M1:coder`), mục tiêu, danh sách file được sửa, tiêu chí đạt và lệnh kiểm tra. Description của tool `task` **phải bắt đầu bằng task_key** — plugin nhúng marker `[run_id/task_key]` để đối soát và từ chối dispatch thiếu task_key.
3. Chỉ một coder ghi trên cùng worktree tại một thời điểm. Coder xong mới review/test; hai bước này chạy song song được nếu không xung đột artifact.
4. Cùng việc: tiếp tục session cũ (truyền `task_id` lấy từ checkpoint). Việc khác: tạo session mới. Worker không tự sinh worker (đã `task: deny`).
5. Kết quả worker phải gồm: trạng thái, file thay đổi, bằng chứng/lệnh test, lỗi còn lại, bước tiếp theo. Tối đa 3 attempt mới cho một task rồi `blocked`; resume cùng session không tính attempt mới.
6. Orchestrator xác minh kết quả (đọc diff, đối chiếu tiêu chí) trước khi chốt `done` qua tool `checkpoint` kèm `evidence`. `session.idle` không đồng nghĩa task đạt yêu cầu. Sau khi chốt: cập nhật `PLAN.md` và checkpoint (`next`).

## Resume và fallback

- Sau gián đoạn: plugin reconcile tự phục hồi main/child ID khi khởi động lại. Task `error` với ghi chú interrupted → resume bằng `task_id` cũ. Session đã mất → xác minh phần đã làm bằng `git diff`, rồi dispatch mới (plugin tự lưu ID cũ vào `history`).
- Lỗi mạng/provider tạm thời: retry tối đa 2 lần có backoff. Sol vẫn lỗi → người dùng chuyển Opus:
  `opencode --session <MAIN_SESSION_ID> --agent orchestrator --model xkiro/anthropic/claude-opus-5`
- Lịch sử không tương thích → tạo main mới: đọc plan/memory/checkpoint, mở session orchestrator mới, rồi `checkpoint` action `adopt` với `run_id` cũ để lưu liên kết ID.
- Người dùng hủy, từ chối quyền, lỗi xác thực: đánh dấu `blocked`/`cancelled`, không tự retry/fallback để vượt qua. Chạy lại test trước khi tiếp tục.
- Không mở hai instance OpenCode cùng lúc trên một worktree khi workflow đang chạy — sẽ gây đụng độ state/dispatch.

## Lệnh thủ công (thay giá trị trong `<…>`)

```bash
opencode session list --format json
opencode --session <MAIN_SESSION_ID> --agent orchestrator
opencode --session <MAIN_SESSION_ID> --agent orchestrator --model xkiro/anthropic/claude-opus-5
```

Không dùng `--continue` khi quản lý nhiều workflow vì có thể chọn nhầm phiên.

## Chung

- Không commit `.opencode/state/`, secret, API key.
- Vision/MCP giữ nguyên cấu hình hiện có; không mở rộng trong v3.
- State machine của task: `pending → dispatching → running → returned → done`, nhánh `error/blocked/cancelled`; chỉ orchestrator được đặt `done`.
