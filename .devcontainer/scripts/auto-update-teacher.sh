#!/bin/bash

set -u

echo "========================================"
echo "🔄 檢查老師最新任務..."
echo "========================================"

# 確認目前是在 Git repository
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "❌ 目前不是 Git repository，停止更新。"
    exit 1
fi

# 確認 teacher remote 存在
if ! git remote get-url teacher >/dev/null 2>&1; then
    echo "❌ 找不到 teacher remote。"
    echo "請先確認：git remote -v"
    exit 1
fi

# 抓取老師最新版本
echo "📥 正在取得老師最新版本..."

if ! git fetch teacher; then
    echo "❌ 無法取得老師最新版本。"
    echo "請檢查網路或 GitHub 權限。"
    exit 1
fi

# 取得目前版本與老師版本
LOCAL=$(git rev-parse HEAD)
TEACHER=$(git rev-parse teacher/main)

# 檢查老師最新版本是否已經包含在目前版本中
if git merge-base --is-ancestor "$TEACHER" "$LOCAL"; then
    echo "✅ 老師最新任務已經在你的版本中，不需要更新。"
    exit 0
fi

echo "🆕 發現老師的新任務！"
echo "目前版本：$LOCAL"
echo "老師版本：$TEACHER"

# 建立安全備份分支
BACKUP_BRANCH="backup-before-teacher-update-$(date +%Y%m%d-%H%M%S)"

echo "🛡️ 建立安全備份：$BACKUP_BRANCH"

if ! git branch "$BACKUP_BRANCH"; then
    echo "❌ 無法建立安全備份分支。"
    echo "為了保護你的程式碼，停止更新。"
    exit 1
fi

# 判斷是否有尚未提交的修改
STASH_CREATED=false

if ! git diff --quiet || ! git diff --cached --quiet; then

    echo "📦 發現你有尚未提交的修改。"
    echo "正在暫存你的程式碼..."

    STASH_NAME="student-work-before-teacher-update-$(date +%Y%m%d-%H%M%S)"

    if ! git stash push -u -m "$STASH_NAME"; then
        echo "❌ 無法暫存你的程式碼。"
        echo "你的程式碼沒有被覆蓋。"
        exit 1
    fi

    STASH_CREATED=true

    echo "✅ 你的修改已安全暫存。"
fi

# 合併老師最新版本
echo "🔀 正在合併老師最新任務..."

if ! git merge teacher/main --no-edit; then

    echo ""
    echo "========================================"
    echo "⚠️ 發生合併衝突！"
    echo "========================================"
    echo ""
    echo "為了保護你的程式碼，自動更新已停止。"

    # 取消這次合併
    git merge --abort 2>/dev/null || true

    # 還原學生自己的修改
    if [ "$STASH_CREATED" = true ]; then
        echo "📦 正在還原你的程式碼..."

        if ! git stash pop; then
            echo "⚠️ 還原修改時發生衝突。"
            echo "請不要執行 reset --hard。"
        fi
    fi

    echo ""
    echo "🛡️ 更新前版本仍然保存在："
    echo "   $BACKUP_BRANCH"
    echo ""
    echo "請把這個畫面貼給老師或 ChatGPT 協助處理。"

    exit 1
fi

# 還原學生原本的修改
if [ "$STASH_CREATED" = true ]; then

    echo "📦 正在還原你自己的程式碼..."

    if ! git stash pop; then

        echo ""
        echo "========================================"
        echo "⚠️ 你的程式碼與老師版本發生衝突"
        echo "========================================"
        echo ""
        echo "為了保護你的程式碼，沒有強制覆蓋。"
        echo ""
        echo "🛡️ 更新前備份：$BACKUP_BRANCH"
        echo ""
        echo "請執行："
        echo "git status"
        echo ""
        echo "然後把結果貼給我。"

        exit 1
    fi
fi

echo ""
echo "========================================"
echo "✅ 老師最新任務更新完成！"
echo "========================================"
echo ""
echo "🛡️ 更新前備份：$BACKUP_BRANCH"
echo "📌 目前版本：$(git rev-parse --short HEAD)"
echo ""
echo "你的程式碼已受到保護。"
