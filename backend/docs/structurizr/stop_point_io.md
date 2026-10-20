# StopPoint I/O 定義

## StopPointHandlers.GetByRange

### 入力

- `lat: number`
  - 検索中心の緯度
- `long: number`
  - 検索中心の経度
- `radius: number`
  - 検索半径（m）
- `limit: integer`
  - 取得件数の上限
  - 未指定時は `500`

### 出力

- `[]StopPointRangeResponse`
  - 範囲内で取得した停止点の一覧
- `uniqueKey: string`
  - 停止点の一意識別子
- `az: string`
  - 停止点に対応する属性値
- `lat: string`
  - 停止点の緯度
- `long: string`
  - 停止点の経度
- `directionStatus: string`
  - `az` の確からしさ（`unknown` / `inferred` / `confirmed`）

## StopPointHandlers.GetByID

### 入力

- `id: string`
  - 取得対象の停止点ID

### 出力

- `StopPointDetailResponse`
  - ID指定で取得した単一の停止点
- `uniqueKey: string`
  - 停止点の一意識別子
- `az: string`
  - 停止点に対応する属性値
- `lat: string`
  - 停止点の緯度
- `long: string`
  - 停止点の経度
- `directionStatus: string`
  - `az` の確からしさ（`unknown` / `inferred` / `confirmed`）

## StopPointService.GetByRange

### 入力

- `latitude: float64`
  - 検索中心の緯度
- `longitude: float64`
  - 検索中心の経度
- `radiusMeters: int`
  - 検索半径（m）
- `limit: int`
  - 取得件数の上限
  - 未指定時は `500`

### 出力

- `[]StopPoint`
  - 範囲検索で取得した停止点一覧
- `uniqueKey: string`
  - 停止点の一意識別子
- `az: string`
  - 停止点に対応する属性値
- `latitude: float64`
  - 停止点の緯度
- `longitude: float64`
  - 停止点の経度
- `directionStatus: string`
  - `az` の確からしさ（`unknown` / `inferred` / `confirmed`）

## StopPointService.GetByID

### 入力

- `id: string`
  - 取得対象の停止点ID

### 出力

- `StopPointDetail`
  - ID指定で取得した単一の停止点（`StopPoint` と同じ項目を持つ別の型）
- `uniqueKey: string`
  - 停止点の一意識別子
- `az: string`
  - 停止点に対応する属性値
- `latitude: float64`
  - 停止点の緯度
- `longitude: float64`
  - 停止点の経度
- `directionStatus: string`
  - `az` の確からしさ（`unknown` / `inferred` / `confirmed`）

## StopPointRepository.FindByRange

### 入力

- `latitude: float64`
  - 検索中心の緯度
- `longitude: float64`
  - 検索中心の経度
- `radiusMeters: int`
  - 検索半径（m）
- `limit: int`
  - 取得件数の上限

### 出力

- `[]StopPoint`
  - PostGIS による範囲検索結果
- `uniqueKey: string`
  - 停止点の一意識別子
- `az: string`
  - 停止点に対応する属性値
- `latitude: float64`
  - 停止点の緯度
- `longitude: float64`
  - 停止点の経度
- `directionStatus: string`
  - `az` の確からしさ（`unknown` / `inferred` / `confirmed`）

## StopPointRepository.FindByID

### 入力

- `id: string`
  - 取得対象の停止点ID

### 出力

- `StopPointDetail`
  - ID指定で取得した単一の停止点（`StopPoint` と同じ項目を持つ別の型）
- `uniqueKey: string`
  - 停止点の一意識別子
- `az: string`
  - 停止点に対応する属性値
- `latitude: float64`
  - 停止点の緯度
- `longitude: float64`
  - 停止点の経度
- `directionStatus: string`
  - `az` の確からしさ（`unknown` / `inferred` / `confirmed`）

## StopPointModel

### 入力

- なし
  - 共有モデル定義のみを担当する

### 出力

- `StopPoint`
  - Service / Repository 間で共有する内部モデル
- `GetByRangeInput`
  - 範囲検索用の内部入力モデル
- `GetByIDInput`
  - ID検索用の内部入力モデル
- `StopPointRangeResponse`
  - Handler が返す範囲検索API用の出力モデル
- `StopPointDetailResponse`
  - Handler が返すID検索API用の出力モデル
