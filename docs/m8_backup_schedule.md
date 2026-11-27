# 정기 백업 일정 (절차와 예시만, 설치하지 않음)

저장소는 백업을 일정대로 만들어 주지 못한다. 여기에는 **스크립트와 절차, launchd 예시**만 있고, 이 저장소는 아무것도 설치하지 않는다. 등록은 사용자 결정이다.

## 스크립트
`tools/scheduled_backup.sh [--verify]`
- 운영 DB(`postgresql:///inazina`)를 읽기 전용 `pg_dump`로 `database/backups/inazina-pg-<시각>.dump`에 저장한다(규칙 7: DSN이 스크립트 안에 적혀 있다). 보관 개수는 `database/backup.py`의 기본값을 따른다.
- `--verify`를 주면 새 덤프를 임시 DB로 복원해 필수 표·외래키를 점검하고 임시 DB를 지운다(`--expect-dbname inazina`로 다른 DB의 덤프를 거부).
- 마지막에 `tools/backup_check.py`가 최신성을 확인한다. 로그는 `logs/backup.log`.

## 권장 일정
- 매일 새벽 1회 덤프, 주 1회 `--verify`.
- 큰 반영(스키마, 등록부, 관할 기준) 직전에는 수동으로 한 번 더 실행하고 `docs/m8_prod_migration_runbook.md`의 절차를 따른다.
- 화면의 '백업 최신성'은 48시간을 넘기면 경고한다. 일정이 어긋나면 이 경고로 알게 된다.

## launchd 예시 (설치하지 않음)
`~/Library/LaunchAgents/kr.inazina.backup.plist`에 아래를 두고 `launchctl load`로 등록하는 것은 사용자가 직접 정할 일이다. 경로는 자기 환경에 맞게 바꾼다.
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>kr.inazina.backup</string>
  <key>ProgramArguments</key><array>
    <string>/Users/USER/Desktop/inazina/tools/scheduled_backup.sh</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>10</integer></dict>
  <key>StandardErrorPath</key><string>/Users/USER/Desktop/inazina/logs/backup.launchd.log</string>
</dict></plist>
```
주 1회 검증은 `<key>Weekday</key><integer>0</integer>`를 더하고 인자에 `--verify`를 추가한 두 번째 plist로 만든다. 맥이 잠자기 상태면 실행되지 않으니 놓친 백업은 위 경고로 확인한다.

## 한계
이 저장소는 백업이 실제로 만들어졌는지를 사후에 알릴 뿐 보장하지 않는다. 오프사이트 보관, 암호화, 복구 시간 측정은 포함하지 않는다.
