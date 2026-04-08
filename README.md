# computer-use-training-generator

외부 teacher 모델의 자연어 응답을 먼저 만들고, 필요하면 teacher가 그 응답을 다시 순차 chunk로 분할한 뒤
`qwen-computer-use-agent`에 조각별로 넣고,
`computer-use-raw-python-executor` endpoint를 통해 실제 GUI 실행을 발생시킨 뒤
step별 학습 샘플을 수집하는 generator repo입니다.

수집 단위는 기본적으로 아래 5종 세트입니다.

- teacher natural-language response
- 실행 전 캡처
- agent가 생성한 Python code
- 실행 후 캡처
- success/fail + 짧은 실패 이유

## 기본 구조

```text
teacher prompt
  -> external teacher command
  -> teacher response text
  -> teacher chunk plan JSON
  -> qwen-computer-use-agent --prompt "<chunk 1>"
  -> qwen-computer-use-agent --prompt "<chunk 2>"
  -> ...
  -> agent run artifacts (multiple runs)
  -> step JSONL + before/after PNG extraction + per-chunk metadata
```

이 repo는 agent나 executor를 직접 재구현하지 않습니다.
실제로는 외부 명령을 호출합니다.

- teacher: 임의의 외부 CLI
- agent: `qwen-computer-use-agent`
- executor: agent bootstrap 시 전달하는 `--endpoint`

## 빠른 시작

설치:

```bash
cd /home/kss930/model-projects/gui-owl-8B-think-1.0.0/computer-use-training-generator
/home/kss930/model-projects/gui-owl-8B-think-1.0.0/.venv/bin/python -m pip install -e .
```

기본 config:
- [config/generator.default.json](/home/kss930/model-projects/gui-owl-8B-think-1.0.0/computer-use-training-generator/config/generator.default.json)

예시:

```bash
/home/kss930/model-projects/gui-owl-8B-think-1.0.0/.venv/bin/training-generator \
  run-session \
  --task "안드로이드 설치방법" \
  --teacher-command-template 'codex exec "{prompt}"'
```

위 흐름은:
1. teacher에게 `안드로이드 설치방법`을 질문
2. teacher 응답을 teacher가 다시 순차 chunk로 분할
3. 각 chunk를 agent prompt로 순차 실행
4. 여러 agent run dir를 한 세션으로 수집
5. step별 JSONL과 PNG를 저장

## 결과물

기본 output은 `data/sessions/<session-id>/` 아래에 생깁니다.

- `teacher.json`
- `agent_bootstrap.json`
- `teacher_plan.json`
- `agent_runs/*.prompt.json`
- `agent_runs/*.json`
- `session.json`
- `samples.jsonl`
- `images/*.png`

`samples.jsonl` 각 row는 대략 이런 형태입니다.

```json
{
  "session_id": "20260406-...",
  "task": "안드로이드 설치방법",
  "teacher_prompt": "안드로이드 설치방법",
  "teacher_text": "...외부 teacher 응답...",
  "chunk_index": 1,
  "chunk_count": 4,
  "chunk_id": "chunk-001",
  "chunk_title": "공식 다운로드 페이지 열기",
  "chunk_success_hint": "카카오톡 공식 페이지가 브라우저에 열림",
  "step_id": "step-000",
  "request_kind": "generate",
  "before_image_path": "images/step-000.before.png",
  "after_image_path": "images/step-000.after.png",
  "target_code": "import ...",
  "agent_raw_text": "...",
  "outcome": "success",
  "failure_type": null,
  "failure_text": null
}
```

## teacher command template

`--teacher-command-template`에는 `{prompt}` placeholder를 넣을 수 있습니다.

예:

```bash
--teacher-command-template 'codex exec "{prompt}"'
```

template 안에 `{prompt}`가 없으면 마지막 인자로 teacher prompt를 자동으로 붙입니다.

## teacher 분할

기본값으로 `run-session`은 teacher 원문을 다시 teacher에게 넣어 순차 chunk JSON으로 분할합니다.

기본 config 키:
- `teacher_split_enabled`
- `teacher_split_timeout_s`

각 chunk는:
- `chunk_id`
- `title`
- `agent_prompt`
- `success_hint`

형태로 저장되고, agent에는 `agent_prompt`가 1조각씩 순차적으로 들어갑니다.
분할 개수는 기본적으로 고정하지 않고, teacher가 task 성격에 맞게 정한 chunk 수를 그대로 사용합니다.

## 기존 run만 수집

이미 생성된 agent run dir가 있으면 teacher 텍스트와 함께 dataset만 다시 만들 수 있습니다.

```bash
/home/kss930/model-projects/gui-owl-8B-think-1.0.0/.venv/bin/training-generator \
  collect-run \
  --run-dir /path/to/agent/data/runs-qwen35/<run-id> \
  --task "안드로이드 설치방법" \
  --teacher-text-file /path/to/teacher.txt
```
