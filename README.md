# 専門論文 Slack 自動収集システム

## 最初に行うこと

この版は、英語Abstractと画像をSlackへ表示せず、OpenAI APIで作成した**日本語約100字の要約**を表示します。

OpenAlexから公開日が過去7日以内の論文を取得し、未投稿のScore 15以上をスコア順に1回最大5本選びます。選ばれた論文だけをOpenAIへ送り、3時間ごとの8回がすべて動けば理論上は1日最大40本です。基準と除外媒体は`config.yaml`だけで変更できます。

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

### 投稿数とスコア基準

```yaml
posting:
  minimum_score: 15
  maximum_posts_per_run: 5
```

取得対象期間：

```yaml
runtime:
  lookback_publication_days: 7
```

毎回、公開日が過去7日以内の論文を取得してスコアを計算し、投稿済み論文を除外します。Score 15以上をスコア順、同点なら公開日の新しい順に並べ、上位5本を投稿します。候補が5本未満なら存在する分だけ投稿し、0本なら投稿せず正常終了します。

```text
Total score = keyword score + journal score - exclusion penalty
```

投稿ルール：

- **Score 15以上**だけをスコア順に選びます。
- **Score 14以下**は投稿しません。
- 1回の上限は5本です。日次上限は設けず、3時間ごとの運用から1日の理論上限が40本になります。
- 選択処理はOpenAI APIを呼ぶ前に完了します。**選ばれた最大5本だけ**を要約します。
- 選択済み論文の要約やSlack投稿が失敗しても、その回に未選択の次点論文を追加要約しません。未投稿論文は次回に再試行します。

### 除外媒体

```yaml
excluded_sources:
  - canonical: JCIS Open
    aliases: [JCIS Open, Journal of Colloid and Interface Science Open]
  - canonical: Figshare
    aliases: [Figshare]
    doi_prefixes: [10.6084/m9.figshare.]
  - canonical: Research Square
    aliases: [Research Square]
    doi_prefixes: [10.21203/rs.]
  - canonical: arXiv
    aliases: [arXiv, arXiv (Cornell University)]
    doi_prefixes: [10.48550/arXiv.]
```

ここに登録した媒体はスコアに関係なく除外します。媒体名は大文字小文字、空白、句読点の違いを無視して照合し、`doi_prefixes`に一致する論文も除外します。

### AI要約

```yaml
ai_summary:
  model: gpt-5.6-terra
  reasoning_effort: none
  target_characters: 100
  minimum_characters: 85
  maximum_characters: 135
  max_output_tokens: 2048
  retry_max_output_tokens: 8192
```

標準では、専門的な要約品質と費用のバランスを取るため`gpt-5.6-terra`を使います。より高い品質を優先する場合は`gpt-5.6-sol`へ変更できます。

要約ルール：

- 日本語一～二文、目標100字、許容85～135字
- 研究対象、主要な方法または設計上の新規性、最重要の結果を含める
- Abstractにない推測、誇張、評価を加えない
- **化合物名、材料名、分子名、反応名、略語、分子式は英語表記のままで構わない**
- 英語名の前後は自然な日本語の助詞・述語でつなぐ
- 文を途中で切らず、最後は必ず`。`で閉じる
- 数値や名詞句だけで終わる文、逐語訳調の不自然な文は採用しない

APIの`max_output_tokens`には、画面に見えないreasoning tokenも含まれます。旧版の`512`では、要約本文を書く前に上限へ達して`status=incomplete`になることがありました。この版では、まず`reasoning_effort: none`で実行し、上限到達時だけ自動的に`retry_max_output_tokens`で再試行します。

日本語案が字数、句点、文の完結性などの検査に通らない場合は、同じAbstractから1回だけ書き直します。それでも通らなければAI生成の英語要約を表示します。API自体が失敗した場合も、Abstractの完結した英文をフォールバック表示するため、通常は論文投稿を止めません。

---

## Slack表示

各論文は1つのSlackメッセージにまとめます。

```text
論文タイトル
著者
ジャーナル | 公開日
Score: 15, Matched keywords: #macrocycle #supramolecular

要約
日本語約100字の要約

［論文を開く］
──────────
```

英語Abstract、Graphical Abstract、代替画像は表示しません。1論文が1メッセージなので、画像投稿で生じていた論文間の位置ずれもありません。

## Abstractを取得できない場合

OpenAlexにAbstractがなければCrossrefを試します。どちらからも取得できない論文は、タイトルだけから推測せず投稿を見送り、次回以降に再試行します。

## APIエラー時

OpenAI APIの一時的な429・5xxエラーは自動再試行します。日本語生成に失敗しても英語フォールバックへ移るため、Abstractが存在する論文は原則として投稿を継続します。

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
