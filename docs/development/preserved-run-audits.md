# 현재 코드의 보존 원본 호환 검사

2026-11-16 · 과거 고정 코드 재현과 별도의 호환 검사

[기준값 registry](../../protocols/reproductions/preserved-run-audits-v1.json)는 `7f4d0f3`의 실제 Git worktree에서 원본 10개를 감사한 **전체 반환 JSON의 canonical SHA-256**을 기록한다. 원본 log·저장 summary의 해시도 확인하며 현재 src가 옛 파일 해시와 같을 것을 요구하지 않는다.

```bash
uv run --frozen python scripts/audit_preserved_runs.py
uv run --frozen python scripts/audit_preserved_runs.py --output outputs/NEW_AUDIT.json
```

원본 위치가 다르면 `--inputs-root /path/to/restored-repository`를 사용한다. 모든 등록 원본이 있어야 통과하며 없거나 다른 것은 생략하지 않는다. 출력 파일은 새 경로를 요구하고 보존된 `runs/` 아래에 쓰지 않는다.

| 범위 | 원본 수 |
| --- | --- |
| 결정 재전송 120요청 | 1 |
| 무기억 기준선 | 1 |
| 전달 비교 48회 | 1 |
| guided 기억 연결 | 1 |
| 자율 조회 파일럿 | 2 |
| 무기억 연결 확인 | 4 |

실패로 끝난 연결 확인도 포함한다. 전달 비교는 기존 감사의 저장 summary 전체 대조에 더해 반환 JSON 전체 해시도 비교한다. 경로·파일 수·크기·mtime_ns를 전후 확인하며 감사가 원본 inventory를 바꾸면 거절한다. blob bytes의 무결성 검사는 각 감사의 독립 읽기 경로가 수행한다. 새 모델·catalog 호출은 없다.

호환 의미가 바뀌는 새 분석은 새 버전으로 기록하고, 이 기준값을 새 결과에 맞춰 덮어쓰지 않는다. 당시 판정·주석을 정확히 재현하려면 [고정 commit 실행기](historical-reproduction.md)를 사용한다. 새 인수 판정에는 `versioned_screen_reply`의 `airline-argument-screen/v2-explicit-day-offset` 표시를 사용한다. 이전 120요청 출력은 기존 모양으로 유지한다.
