# 専門論文 Slack 自動収集システム（無料翻訳版）

3時間ごとにOpenAlexを検索し、journal scoreとkeyword scoreで選別した論文をSlackへ投稿します。

Slack投稿には以下を含みます。

1. 論文タイトル
2. 著者
3. 雑誌名・公開日
4. 取得できた場合はGraphical Abstract
5. Abstract日本語訳（全文・機械翻訳）
6. Abstract original（全文）
7. 関連すると判定した理由とタグ
8. 「論文を開く」ボタン

日本語訳には、Hugging Faceで公開されている `Helsinki-NLP/opus-mt-en-jap` をGitHub Actions上で実行します。OpenAI APIは使いません。

## 必要なアカウント

- Slack workspace
- GitHub account
- OpenAlex account/API key

OpenAIアカウント、OpenAI API key、翻訳APIの課金設定は不要です。

## GitHub Secrets

Repositoryで次を開きます。

```text
Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

次の4件を登録します。

| Secret名 | 内容 |
|---|---|
| `OPENALEX_API_KEY` | OpenAlexの無料API key |
| `SLACK_BOT_TOKEN` | `xoxb-...`で始まる新しいBot token |
| `SLACK_CHANNEL_ID` | 投稿先の`C...` |
| `CONTACT_EMAIL` | Crossrefへの連絡先メールアドレス |

`OPENAI_API_KEY`は登録しません。

## Slack設定

1. Slack App管理画面でAppを作る。
2. `OAuth & Permissions`でBot Token Scope `chat:write`を追加する。
3. AppをWorkspaceへインストールする。
4. 新しく発行された`xoxb-...`をGitHub Secret `SLACK_BOT_TOKEN`へ直接登録する。
5. 投稿先チャンネルで `/invite @Paper Watch` を実行する。
6. チャンネルIDをGitHub Secret `SLACK_CHANNEL_ID`へ登録する。

## OpenAlex設定

1. OpenAlexの無料アカウントを作成する。
2. SettingsからAPI keyを取得する。
3. GitHub Secret `OPENALEX_API_KEY`へ登録する。

## Repositoryの作成とアップロード

1. GitHubで`paper-slack-bot`というRepositoryを作る。
2. ZIPを展開する。
3. 外側のフォルダではなく、その中身をすべてRepositoryへアップロードする。
4. `.github/workflows/paper-watch.yml`が存在することを確認する。
5. `Settings → Actions → General → Workflow permissions`で`Read and write permissions`を選ぶ。

## 初回実行

```text
Actions
→ Paper Watch
→ Run workflow
```

初回は英日翻訳モデルをダウンロードします。以後はGitHub Actionsのcacheを利用します。

## 定期実行

`.github/workflows/paper-watch.yml`には次が設定されています。

```yaml
- cron: "17 */3 * * *"
```

3時間ごとに、基準を満たした未投稿論文を原則すべて投稿します。1日30報は目安であり、ハード上限ではありません。

## Score

```text
Total = keyword score + journal score - exclusion penalty
投稿条件：Total >= 15 かつ keyword score >= 3
```

- Tier S: +10
- Tier A: +7
- Tier B: +4
- Core: Title +10 / Abstract +5
- Strong: Title +6 / Abstract +3
- Exclude: Title -12 / Abstract -8

雑誌・キーワード・閾値は`config.yaml`だけで編集できます。

## 翻訳について

翻訳は無料のオープンソース機械翻訳です。原文abstractも同時に表示されるため、専門用語や化学名の訳は原文と照合してください。Abstractを取得できない論文については、日本語訳も表示できません。

## Graphical Abstractについて

Graphical Abstractの統一APIはないため、出版社ページにGraphical AbstractまたはTOC graphicとして明示された公開画像だけを表示します。取得できない場合は画像欄を省略します。認証やpaywallの回避は行いません。
