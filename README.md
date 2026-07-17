# 専門論文 Slack 自動収集システム

## 最初に行うこと

この版は、英語Abstractと画像をSlackへ表示せず、OpenAI APIで作成した**日本語約100字の要約**を表示します。

### 1. GitHub Secretを1件追加

Repositoryで次を開きます。

```text
Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

登録内容：

```text
Name: OPENAI_API_KEY
Secret: OpenAI Platformで発行したAPI key
```

API keyをREADME、コード、Slack、このチャットへ貼らないでください。

### 2. Workflowを更新

MacやブラウザからZIPをアップロードすると、`.github`フォルダが反映されないことがあります。
GitHub上で次を開きます。

```text
.github
→ workflows
→ paper-watch.yml
→ 鉛筆アイコン
```

内容をすべて削除し、ZIP直下の **`paper-watch.workflow.yml`** の内容を貼り付けてCommitしてください。
この変更でGitHub Actionsへ`OPENAI_API_KEY`が渡されます。

### 3. 不要になった旧ファイル

旧版から次のファイルが残っていれば削除して構いません。

```text
paper_watch/article_image.py
```

画像を使わないため、Slack Appの`files:write`権限、Playwright、Chromiumは不要です。`chat:write`だけで投稿できます。

---

## 普段編集する場所

キーワード、ジャーナルTier、ドボンキーワード、スコア、投稿閾値、AIモデル、要約文字数は、すべて **`config.yaml`** で編集できます。通常はPythonコードを触りません。

GitHubで：

```text
Code
→ config.yaml
→ Edit this file
→ 編集
→ Commit changes...
```

### 重要キーワード

```yaml
keywords:
  core:
    terms:
      - molecular nanotube
      - carbon nanobelt
  strong:
    terms:
      - macrocycle
      - supramolecular
```

### ドボンキーワード

```yaml
exclusion_keywords:
  terms:
    - peptide
    - protein
    - drug delivery
```

### ジャーナルTier

```yaml
journal_tiers:
  tier_a:
    score: 7
    journals:
      - canonical: ChemRxiv
        aliases: [ChemRxiv, ChemRxiv Preprints]
```

`ChemRxiv`はTier Aに登録済みです。

### 投稿閾値

```yaml
posting:
  minimum_total_score: 15
  minimum_keyword_score: 3
```

```text
Total score = keyword score + journal score - exclusion penalty
```

### AI要約

```yaml
ai_summary:
  model: gpt-5.6-terra
  reasoning_effort: low
  target_characters: 100
  minimum_characters: 90
  maximum_characters: 120
```

標準では`gpt-5.6-terra`を使います。最高品質を優先するときは`gpt-5.6-sol`、費用を抑えるときは`gpt-5.6-luna`へ、`model`の1行だけ変更できます。

要約ルール：

- 日本語一文、目標100字、原則90～120字
- 研究対象、実施内容、主要結果または意義を含める
- Abstractにない推測、誇張、評価を加えない
- **化合物名、材料名、分子名、反応名、略語、分子式は英語のままでよい**
- 数値、符号、化学式、立体化学表記は原文を尊重する
- 「本研究では」などの冗長な導入、見出し、引用符を付けない

文字数が範囲外の場合は、同じAbstractを使って1回だけ自動修正します。この回数は`length_revision_attempts`で変更できます。

---

## Slack表示

各論文は1つのSlackメッセージにまとめます。

```text
論文タイトル
著者
ジャーナル | 公開日
Score

Matched keywords
#macrocycle #supramolecular

要約
日本語約100字の要約

［論文を開く］
──────────
```

英語Abstract、Graphical Abstract、代替画像は表示しません。1論文が1メッセージなので、画像投稿で生じていた論文間の位置ずれもありません。

## Abstractを取得できない場合

OpenAlexにAbstractがなければCrossrefを試します。どちらからも取得できない論文は、タイトルだけから推測せず投稿を見送り、次回以降に再試行します。

## APIエラー時

OpenAI APIの一時的な429・5xxエラーは自動再試行します。要約に失敗した論文は投稿済みに記録しないため、次の定期実行で再試行されます。他の論文の処理は続行します。

## GitHub Secrets

必要なSecretは5件です。

| Secret名 | 内容 |
|---|---|
| `OPENALEX_API_KEY` | OpenAlex API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `SLACK_BOT_TOKEN` | `xoxb-...`で始まるBot token |
| `SLACK_CHANNEL_ID` | 投稿先チャンネルの`C...` |
| `CONTACT_EMAIL` | OpenAlex/Crossrefへの連絡先メールアドレス |

## Slack App

必要なBot Token Scope：

```text
chat:write
```

投稿先チャンネルへBotを招待します。

```text
/invite @Paper Watch
```

## 実行

```text
Actions
→ Paper Watch
→ Run workflow
```

3時間ごとの定期実行は次のcronです。

```yaml
- cron: "17 */3 * * *"
```

## 投稿済みデータ

`data/state.json`へ投稿済みDOIと要約を保存し、重複投稿を防ぎます。
調整中に同じ論文を再投稿したい場合だけ`data`を削除してください。

## 設定検証

Actionsは投稿前に`pytest -q`を実行します。キーワード重複、Tier重複、要約文字数設定の矛盾などがある場合は、Slack投稿前に停止します。
