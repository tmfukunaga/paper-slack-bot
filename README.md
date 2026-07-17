# 専門論文 Slack 自動収集システム

## 最初に行うこと

この版は、英語Abstractと画像をSlackへ表示せず、OpenAI APIで作成した**日本語約100字の要約**を表示します。

投稿数は、Score 15以上を優先し、Score 11～14を1回3本・1日30本までの補充枠として扱います。さらに、1回の実行ではスコア順の最大10本だけを選び、選ばれた論文だけをOpenAIへ送ります。基準は`config.yaml`だけで変更できます。

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
  guaranteed_score: 15
  conditional_minimum_score: 11
  target_posts_per_run: 3
  target_posts_per_day: 30
  maximum_posts_per_run: 10
  minimum_keyword_score: 3
```

```text
Total score = keyword score + journal score - exclusion penalty
```

投稿ルール：

- **Score 15以上**：最優先で選びます。ただし、1回の実行全体では上位10本までです。11本目以降は次回へ回ります。
- **Score 11～14**：補充枠です。Score 15以上を含む選択数が3本未満、かつ当日の投稿成功数が30本未満の間だけ、スコア順に補充します。
- **Score 10以下**：投稿しません。
- 選択処理はOpenAI APIを呼ぶ前に完了します。**選ばれた最大10本だけ**を要約します。
- 選択済み論文の要約やSlack投稿が失敗しても、その回に未選択の次点論文を追加要約しません。未投稿論文は次回に再試行します。
- 1日30本はScore 11～14にだけ適用する**ソフト上限**です。Score 15以上には適用しませんが、1回10本のハード上限は適用します。

例：

```text
24, 18, 15, 14, 13 → 24, 18, 15を選択
14, 13, 12, 11, 10 → 14, 13, 12を選択
20, 16, 14, 13 → 20, 16, 14を選択
Score 15以上が12本 → 上位10本だけを選択し、残り2本は次回へ
```

### AI要約

```yaml
ai_summary:
  model: gpt-5.6-sol
  reasoning_effort: none
  target_characters: 100
  minimum_characters: 85
  maximum_characters: 135
  max_output_tokens: 2048
  retry_max_output_tokens: 8192
```

標準では、品質を優先して`gpt-5.6-sol`を使います。費用を抑える場合は`gpt-5.6-terra`へ変更できます。

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
