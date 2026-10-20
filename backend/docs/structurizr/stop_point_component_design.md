# StopPoint コンポーネント設計

## 目的

`GetStopPointByID` と `GetStopPointByRange` を、StopPoint API の最小構成として設計する。
この資料では、友人へ残りの実装を依頼する前提で、インターフェースと責務分離を明確にする。

## コンポーネント構成

- `Entrypoint層`
  - Lambda ハンドラや初期化処理を持つ
  - AWS インフラから渡されたイベントを適切な Handler にルーティングする
- `Handlers`
  - `StopPointHandlers` に属するメソッド群を囲い線付きで表現するセクション
  - `GetByID`
  - `GetByRange`
- `Services`
  - `StopPointService` に属するメソッド群を囲い線付きで表現するセクション
  - `GetByID`
  - `GetByRange`
- `StopPointModel`
  - 各コンポーネント内、またはコンポーネント間で共有する型を定義する
- `StopPointRepository`
  - 永続化と取得のためのインターフェース
  - PostGIS を使った実検索は実装側に閉じ込める

## インターフェース方針

- `StopPointHandlers` は 1 つの構造体として定義する
- 図では `Handlers` の囲い線の中に `GetByID` と `GetByRange` を配置し、同一構造体のメソッドであることを表現する
- `StopPointService` は 1 つの構造体として定義する
- 図では `Services` の囲い線の中に `GetByID` と `GetByRange` を配置し、同一構造体のメソッドであることを表現する
- `StopPointRepository` は `FindByID` と `FindByRange` を提供する
- `Handler` は HTTP を扱うが、`Service` は HTTP に依存しない
- `Repository` 実装は PostGIS を使うが、`Service` は PostGIS を意識しない

## Handlers

`Handlers` セクションは `StopPointHandlers` に属するメソッド群を表す。

### `GetByID`

- `StopPointHandlers.GetByID` を表す
- 停止点ID検索 API のエントリ
- パスまたはクエリから停止点IDを受け取る
- `StopPointModel` の入出力型を利用する
- `Services.GetByID` を呼び出す

### `GetByRange`

- `StopPointHandlers.GetByRange` を表す
- 停止点範囲検索 API のエントリ
- 中心座標、範囲(`m`)、`limit` を受け取る
- `StopPointModel` の入出力型を利用する
- `Services.GetByRange` を呼び出す

## Services

`Services` セクションは `StopPointService` に属するメソッド群を表す。

### `GetByID`

- `StopPointService.GetByID` を表す
- 停止点IDを受け取り、単一の停止点取得ユースケースを実行する
- `StopPointRepository.FindByID` を呼び出す

### `GetByRange`

- `StopPointService.GetByRange` を表す
- 中心座標、範囲(`m`)、`limit` を受け取り、範囲検索ユースケースを実行する
- `limit` のデフォルト値を 500 として補完する
- `StopPointRepository.FindByRange` を呼び出す

## モデル方針

`StopPointModel` には、現時点では最小限の型だけを置く。

- `StopPoint`
  - `uniqueKey`、座標、`az`、`directionStatus`
  - 根拠・出所・データセット版は含めない
- `StopPointDetail`
  - `StopPoint` と同じ項目を持つ別の型
- `GetByRangeInput`
  - `center_latitude`
  - `center_longitude`
  - `range_meter`
  - `limit`

後から属性を増やせるよう、責務を「共有モデル定義」に限定する。

## 範囲検索の責務分離

- `Handler`
  - リクエストから中心座標、範囲、limit を受け取る
- `Service`
  - `limit` のデフォルト値を 500 として補完する
  - Repository へ検索条件を渡す
- `Repository`
  - PostGIS による地理空間検索を実行する
- `DB`
  - バックエンドの外部要素として扱う
  - 地理空間検索責務を持つ

## 補足

- `GetStopPintByRange` ではなく `GetStopPointByRange` に統一する
- 今回の図では、マイグレーションやインフラ構築要素は扱わない
