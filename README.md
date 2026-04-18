# computer-use-training-generator

외부 teacher 모델의 자연어 응답을 먼저 만들고, 필요하면 teacher가 그 응답을 다시 순차 chunk로 분할한 뒤
각 chunk에 read-only verifier를 붙이고,
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
  -> verifier execute on executor endpoint
  -> pass => next chunk / fail => retry current chunk or stop
  -> qwen-computer-use-agent --prompt "<chunk 2>"
  -> verifier execute on executor endpoint
  -> ...
  -> agent run artifacts (multiple runs)
  -> step JSONL + before/after PNG extraction + per-chunk metadata + verify result
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
3. 각 chunk에 verifier DSL을 같이 만든다
4. 각 chunk를 agent prompt로 순차 실행
5. chunk 실행 후 verifier를 executor endpoint에서 직접 실행한다
6. verifier pass면 다음 chunk, fail이면 same-chunk retry 또는 세션 중단
7. 여러 agent run dir를 한 세션으로 수집
8. step별 JSONL과 PNG를 저장

## 결과물

기본 output은 `data/sessions/<session-id>/` 아래에 생깁니다.

- `teacher.json`
- `agent_bootstrap.json`
- `teacher_plan.json`
- `agent_runs/*.prompt.json`
- `agent_runs/*.json`
- `agent_runs/*.verification.json`
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
  "chunk_preconditions": ["브라우저가 실행 중이어야 함"],
  "chunk_verification": {
    "checks": [
      {"kind": "process_exists", "name": "chrome.exe"}
    ]
  },
  "chunk_max_retries": 1,
  "chunk_on_fail": "retry_current_chunk",
  "chunk_attempt": 1,
  "chunk_completed": true,
  "chunk_verification_result": {
    "passed": true,
    "evidence": [
      {"kind": "process_exists", "name": "chrome.exe", "exists": true, "passed": true}
    ]
  },
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

## CLI 사용법

기본 형태:

```bash
/home/kss930/model-projects/gui-owl-8B-think-1.0.0/.venv/bin/training-generator \
  --config config/generator.default.json \
  run-session \
  --task "작업 설명"
```

config 없이 실행하면 현재 작업 디렉터리 기준 `config/generator.default.json`을 찾습니다.

### `run-session`

teacher -> agent -> verifier -> dataset 수집 전체 흐름을 한 번에 실행합니다.

핵심 옵션:

- `--task`
  - 필수값입니다.
  - teacher에 전달할 실제 작업 설명입니다.
- `--teacher-prompt`
  - teacher에 보낼 문구를 `--task`와 다르게 따로 지정할 때 씁니다.
- `--teacher-command-template`
  - 외부 teacher CLI 템플릿입니다.
  - `{prompt}` placeholder를 넣을 수 있습니다.
- `--teacher-timeout-s`
  - teacher 자연어 응답 생성 timeout입니다.
- `--teacher-workdir`
  - teacher CLI를 실행할 작업 디렉터리입니다.
- `--teacher-split-enabled`
  - teacher 원문 응답을 다시 순차 chunk JSON으로 분할하게 강제합니다.
  - config에서 이미 켜져 있으면 보통 다시 줄 필요는 없습니다.
- `--teacher-split-timeout-s`
  - teacher 분할 단계 timeout입니다.
- `--execution-style`
  - teacher chunking과 local fallback이 agent를 어떤 방향으로 유도할지 고릅니다.
  - `python_first`: direct download, 파일 검증, silent/subprocess install 쪽을 우선
  - `gui_first`: 현재 보이는 브라우저/검색 결과/다운로드 UI/installer wizard를 Python GUI 자동화로 이어가는 쪽을 우선
  - raw/qwen agent daemon 재사용 시에도 같은 값을 agent `execution_style` override로 함께 전달합니다.
- `--agent-command`
  - 사용할 agent CLI 경로 또는 이름입니다.
  - 현재는 `computer-use-raw-python-agent`, `qwen-computer-use-agent` 모두 지원합니다.
- `--agent-model-id`
  - bootstrap 시 agent에 넘길 모델 식별자입니다.
  - 이미 daemon이 떠 있는 raw agent 흐름에서는 보통 쓰지 않습니다.
- `--agent-config-path`
  - agent에 넘길 config JSON 경로입니다.
- `--agent-endpoint`
  - executor endpoint입니다.
- `--agent-workdir`
  - agent CLI를 실행할 작업 디렉터리입니다.
- `--agent-bootstrap-timeout-s`
  - agent bootstrap timeout입니다.
- `--agent-prompt-timeout-s`
  - 각 chunk의 agent 실행 timeout입니다.
- `--agent-reasoning-enabled`
  - bootstrap/prompt 호출 때 reasoning 플래그를 켭니다.
- `--chunk-verification-enabled`
  - chunk 실행 후 teacher verifier를 강제로 돌립니다.
  - config에서 켜져 있으면 보통 다시 줄 필요는 없습니다.
- `--chunk-verification-timeout-s`
  - verifier timeout입니다.
- `--skip-bootstrap`
  - agent의 `--model-id` bootstrap 호출을 생략합니다.
  - 이미 daemon을 띄워둔 `computer-use-raw-python-agent`, `qwen-computer-use-agent` 재사용 흐름에서는 이 옵션을 붙이는 것이 맞습니다.
- `--output-dir`
  - 세션 결과를 저장할 루트 디렉터리입니다.
- `--session-outcome`
  - 세션 완료 후 수동 라벨을 `success`, `fail`, `unknown` 중 하나로 지정합니다.
- `--session-note`
  - 세션에 짧은 메모를 남깁니다.
- `--include-unexecuted-steps`
  - executor 산출물이 없는 step도 dataset에 포함합니다.

예시 1: Codex teacher + 이미 띄워둔 raw agent daemon 재사용

```bash
PYTHONPATH=src /home/kss930/model-projects/gui-owl-8B-think-1.0.0/.venv/bin/python \
  -m computer_use_training_generator.cli \
  --config config/generator.codex.gpt54.json \
  run-session \
  --task "dbeaver를 설치해줘" \
  --skip-bootstrap
```

예시 2: Qwen agent daemon 재사용

```bash
PYTHONPATH=src /home/kss930/model-projects/gui-owl-8B-think-1.0.0/.venv/bin/python \
  -m computer_use_training_generator.cli \
  --config config/generator.qwen35.json \
  run-session \
  --task "eclipse를 설치하고 새 Java 프로젝트를 만들어줘" \
  --skip-bootstrap
```

예시 2-1: Qwen agent daemon 재사용, GUI-first 수집

```bash
PYTHONPATH=src /home/kss930/model-projects/gui-owl-8B-think-1.0.0/.venv/bin/python \
  -m computer_use_training_generator.cli \
  --config config/generator.qwen35.gui.json \
  run-session \
  --task "eclipse를 설치하고 새 Java 프로젝트를 만들어줘" \
  --skip-bootstrap
```

예시 3: config 일부만 CLI에서 override

```bash
PYTHONPATH=src /home/kss930/model-projects/gui-owl-8B-think-1.0.0/.venv/bin/python \
  -m computer_use_training_generator.cli \
  --config config/generator.default.json \
  run-session \
  --task "카카오톡을 설치해줘" \
  --teacher-command-template 'codex exec -m gpt-5.4-mini "{prompt}"' \
  --agent-command ../../computer-use-raw-python-agent/.venv/bin/qwen-computer-use-agent \
  --agent-config-path ../../computer-use-raw-python-agent/config/agent.qwen35.default.json \
  --agent-workdir ../../computer-use-raw-python-agent \
  --skip-bootstrap
```

### `collect-run`

이미 만들어진 agent run 디렉터리를 dataset으로만 재수집합니다.
teacher/agent를 다시 실행하지 않고 산출물 변환만 다시 할 때 씁니다.

옵션:

- `--run-dir`
  - 필수값입니다.
  - 기존 agent run 디렉터리 경로입니다.
- `--task`
  - 필수값입니다.
  - 이 run에 대응하는 상위 작업 설명입니다.
- `--teacher-prompt`
  - 원래 teacher에 넣었던 프롬프트를 기록용으로 남깁니다.
- `--teacher-text`
  - teacher 원문 응답을 직접 문자열로 넘깁니다.
- `--teacher-text-file`
  - teacher 원문 응답 파일 경로입니다.
- `--output-dir`
  - 수집 결과 저장 루트입니다.
- `--session-outcome`
  - 세션 라벨을 수동 지정합니다.
- `--session-note`
  - 세션 메모를 남깁니다.
- `--include-unexecuted-steps`
  - executor 산출물이 없는 step도 포함합니다.

예시:

```bash
/home/kss930/model-projects/gui-owl-8B-think-1.0.0/.venv/bin/training-generator \
  collect-run \
  --run-dir /path/to/agent/run-dir \
  --task "dbeaver를 설치해줘" \
  --teacher-text-file /path/to/teacher.txt
```

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
- `preconditions`
- `verification`
- `max_retries`
- `on_fail`

형태로 저장되고, agent에는 `agent_prompt`가 1조각씩 순차적으로 들어갑니다.
분할 개수는 기본적으로 고정하지 않고, teacher가 task 성격에 맞게 정한 chunk 수를 그대로 사용합니다.

## chunk verifier

teacher는 각 chunk마다 read-only verifier를 같이 생성합니다.

허용되는 verifier check kind:
- `path_exists`
- `file_exists_glob`
- `file_size_gt`
- `process_exists`

예시:

```json
{
  "chunk_id": "chunk-002",
  "title": "설치 파일 다운로드",
  "agent_prompt": "공식 다운로드 페이지에서 Windows 설치 파일 다운로드를 완료해라.",
  "success_hint": "설치 파일이 Downloads에 존재하는 상태",
  "preconditions": [
    "공식 다운로드 페이지가 이미 열려 있어야 함"
  ],
  "verification": {
    "checks": [
      {"kind": "file_exists_glob", "pattern": "~/Downloads/KakaoTalk*.exe"},
      {"kind": "file_size_gt", "pattern": "~/Downloads/KakaoTalk*.exe", "bytes": 1000000}
    ]
  },
  "max_retries": 1,
  "on_fail": "retry_current_chunk"
}
```

`run-session`은 각 chunk 실행 후 이 verifier를 executor endpoint에서 직접 실행합니다.

수집 프로필은 기본적으로 둘로 나눌 수 있습니다.
- [generator.qwen35.json](/home/kss930/model-projects/gui-owl-8B-think-1.0.0/computer-use-training-generator/config/generator.qwen35.json): `python_first`
- [generator.qwen35.gui.json](/home/kss930/model-projects/gui-owl-8B-think-1.0.0/computer-use-training-generator/config/generator.qwen35.gui.json): `gui_first`

필요하면 config를 유지한 채 CLI에서 `--execution-style gui_first` 또는 `--execution-style python_first`로 바로 덮어쓸 수도 있습니다.

- verifier 통과: 다음 chunk 진행
- verifier 실패 + `on_fail=retry_current_chunk`: 현재 chunk 재시도
- verifier 실패 + `on_fail=fail_session`: 세션 중단

관련 config 키:
- `chunk_verification_enabled`
- `chunk_verification_timeout_s`

## 기존 run만 수집

이미 생성된 agent run dir가 있으면 teacher 텍스트와 함께 dataset만 다시 만들 수 있습니다.

```bash
/home/kss930/model-projects/gui-owl-8B-think-1.0.0/.venv/bin/training-generator \
  collect-run \
  --run-dir /path/to/agent/data/runs-qwen35/<run-id> \
  --task "안드로이드 설치방법" \
  --teacher-text-file /path/to/teacher.txt
```
