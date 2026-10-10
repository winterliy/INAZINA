#!/bin/zsh

cd /Users/sinjaehyeon/Documents/naconomics/

git add .
CURRENT_TIME=$(date "+%Y-%m-%d %H:%M")
git commit -m "Auto-commit: $CURRENT_TIME"
git push origin main

echo "Git 작업이 완료되었습니다!"
exit