# Как коммитить и публиковать (справка к шагу «Применение»)

Рабочая папка человека неприкосновенна: там могут быть его незакоммиченные правки и параллельные сессии. Все коммиты в репозитории проекта делаются в отдельной рабочей копии.

## Коммит в репозиторий проекта

1. Цель публикации. Целевая ветка — та, куда реально вливают: если инструкции репозитория называют релизную ветку или ветку по умолчанию для PR, берём её, иначе `repo.default`. Не уверен — спроси человека одной строкой.
2. Свежая база: `git -C <root> fetch origin <target>`.
3. Рабочая копия от `origin/<target>`, полная (без sparse checkout, иначе хуки не найдут файлы):
   - `publish: pr` — `git -C <root> worktree add <tmp> -b memory-to-repo origin/<target>` (ветка уже есть — `worktree add <tmp> memory-to-repo`).
   - `publish: direct` — `git -C <root> worktree add --detach <tmp> origin/<target>`.
4. Вставь тексты, `git -C <tmp> commit -m "<что добавлено>, из локальной памяти агента"`.
   Хук упал из-за отсутствующего инструмента — запусти коммит с `PATH=<root>/.venv/bin:<root>/node_modules/.bin:$PATH`; хуки проходят честно, без `--no-verify`. Хук переформатировал файлы — `git add` и коммит ещё раз.
5. Запиши публикацию: `memtool.py branch <root> <ветка или HEAD-коммит> <target>`.
6. `git worktree remove <tmp>` — только когда коммит есть в `git -C <root> log <ветка>`. Для `direct` сначала создай ссылку на коммит: `git -C <root> branch -f memory-to-repo <sha>`.

## Правка файла, который у человека уже изменён

Так бывает с файлами вне worktree: глобальный `CLAUDE.md` в репозитории dotfiles, `CLAUDE.md` в общей папке. Закоммить только свой кусок через временный индекс, не трогая его правки:

```
f=<путь от корня репо>; base=$(mktemp); idx=$(mktemp -u)
git show HEAD:"$f" > "$base"        # затем вставь свой текст в $base
blob=$(git hash-object -w "$base")
GIT_INDEX_FILE=$idx git read-tree HEAD
GIT_INDEX_FILE=$idx git update-index --cacheinfo 100644,$blob,"$f"
c=$(git commit-tree $(GIT_INDEX_FILE=$idx git write-tree) -p HEAD -m "<сообщение>")
git update-ref HEAD $c && git reset -q -- "$f"
```

Рабочий файл человека при этом не меняется, свою вставку допиши в него тоже. Готово, когда `git show HEAD --stat` содержит только твой файл, а `git diff -- "$f"` — только правки человека.

## CLAUDE.local.md

Проверь `git -C <root> check-ignore -q CLAUDE.local.md`. Не игнорируется — один раз добавь `CLAUDE.local.md` в глобальный excludes-файл git (`git config --global core.excludesFile`, по умолчанию `~/.config/git/ignore`): так он скрыт во всех репозиториях сразу, а `.gitignore` проекта не меняется.

## Публикация (финал)

По каждой записи из `memtool.py status` («Ветка … → …»):

- ветка совпадает с целью (коммит прямо в ветке человека, например dotfiles): `git -C <root> push origin <target>`.
- `direct`: `git -C <root> push origin <sha>:refs/heads/<target>`. Отказ non-fast-forward — `fetch`, перенеси коммит `cherry-pick` на свежий `origin/<target>` в worktree и повтори.
- `pr`: `git -C <root> push -u origin memory-to-repo`, затем `gh pr create --repo <owner/repo> --base <target> --head memory-to-repo --title "…" --body "…"`. В описании — что добавлено и что это из локальной памяти агента.
- `none`: ничего, ветка остаётся у человека.

Готово, когда каждая запись опубликована или человек сказал оставить её локально; ссылки на PR покажи списком.
