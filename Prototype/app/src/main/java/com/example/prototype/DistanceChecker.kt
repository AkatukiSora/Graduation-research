package com.example.prototype

import android.content.Context
import android.os.Looper
import android.util.Log
import android.location.Location
import android.media.MediaPlayer
import android.os.Vibrator
import android.os.VibrationEffect

// 選ばれた一時停止標識との距離を確認
// 音声・バイブ通知と一時停止回数のカウントを行うクラス
class DistanceChecker(context: Context) {
    // 一時停止できた回数
    var stopSuccsesCount = 0

    // 通過した一時停止標識の回数
    var stopPointsCount = 0

    // 現在検知対象になっている標識の緯度・経度
    var targetLatitude: Double? = null //目的地緯度
    var targetLongitude: Double? = null //目的地経度

    // 現在地の緯度・経度
    var nowLatitude: Double? = null//現在地緯度
    var nowLongtitude: Double? = null//現在地経度

    // 現在の速度
    var nowSpeed = 100f

    var unStopSign = mutableListOf<String>()

    // この距離以内に入ったら警告を鳴らす
    val detectDistance = 30f //接近を検知する距離

    // 前回計算した標識までの距離
    var previousDistance = -1f //

    // 同じ標識に対して再度カウントしないためのフラグ
    var isChecks = true

    // 現在対象になっている標識のID
    var targetUniqueKey: String = ""

    private val checker = FirstLaunchCheck(context)

    private var mediaPlayer: MediaPlayer? = null //メディアプレイヤー
    private var vibrator: Vibrator? = null //バイブレーション

    // バイブレーションのパターン
    val pattern = longArrayOf(0, 500, 300, 500) //バイブレーションパターン

    //将来的に消す
    private val handler = android.os.Handler(Looper.getMainLooper())

//    private val chackRunnable = object : Runnable //繰り返し実行する処理

    //クラス生成時に自動実行
    init {
////        停止回数生成
//        stopSuccsesCount = 0
        //音声ファイル読み込み
        mediaPlayer = MediaPlayer.create(context,R.raw.alert_tsumugi)
        //ループ再生するように
        mediaPlayer?.isLooping = true
        // バイブレーションを使えるようにする
        vibrator = context.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator

    }
//    private val checkRunnable = object : Runnable {
//    override
    fun run() {
        val results = FloatArray(1)
        // 必要な値がまだ入っていない場合は処理をしない
        targetLatitude ?: return
        targetLongitude ?: return
        nowLongtitude ?: return
        nowLatitude ?: return
        // 現在地から対象標識までの距離を計算する
        Location.distanceBetween(
            nowLatitude!!,
            nowLongtitude!!,
            targetLatitude!!,
            targetLongitude!!,
            results
        )
        val distance = results[0]
//        val bearing = bearing()
//        val radians = Math.toRadians(bearing.toDouble())
//        val roadCheck = Math.abs(cos(radians) * distance)

        Log.d("DistanceCheck", "距離 = $distance")

        // 前回より距離が大きくなった場合は、標識から遠ざかっていると判断する
        if (previousDistance != -1f) {
            if (distance > previousDistance) {
                Log.d("DistanceCheck", "遠ざかっています")
                // 遠ざかり始めたら警告音を止める
                if (mediaPlayer?.isPlaying == true) {
                    //音声アラート停止
                    mediaPlayer?.pause()
                }
                //バイブレーション停止
                vibrator?.cancel()
//                isChecks = false
                //ここで次の一時停止地点を探すようにする
            }
        }
//    Log.e("test","${roadCheck}")
        // まだこの標識を判定していない、かつ30m以内に入った場合
        if (isChecks && distance <= detectDistance /*&& roadCheck <= 5*/) {
            // カウント確認用ログ
            Log.e(
                "COUNT_CHECK",
                "key=$targetUniqueKey distance=$distance speed=$nowSpeed"
            )
            // 標識から10m以内に入ったら、通過対象としてカウントする
            if (distance <= 10f){
                stopPointsCount++
                unStopSign.add(targetUniqueKey)
//                isChecks = false
                // 速度が5以下なら一時停止成功としてカウントする (現在はテスト用の値。本番ではもっと低い値にする予定)
                if (nowSpeed <= 5f){
                    stopSuccsesCount++
                    unStopSign.remove(targetUniqueKey)
                    Log.d("test","一時停止")
                }
                // 同じ標識で何度もカウントしないようにする
                isChecks = false
            }
            Log.d("DistanceCheck", "接近しました！")

            val alertMode = checker.getAlertMode()

            // 警告音が鳴っていなければ再生する
            if (alertMode == FirstLaunchCheck.MODE_SOUND || alertMode == FirstLaunchCheck.MODE_BOTH) {
                if (mediaPlayer?.isPlaying != true) {
                    //音声ファイルを開始時に戻し、再生
                    mediaPlayer?.seekTo(0)
                    mediaPlayer?.start()
                }
            }

            //バイブレーション開始 (androidのバージョンによってコードが違うので分岐)
            if (alertMode == FirstLaunchCheck.MODE_VIBRATION || alertMode == FirstLaunchCheck.MODE_BOTH) {
                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                    vibrator?.vibrate(
                        VibrationEffect.createWaveform(
                            longArrayOf(0, 500, 300, 500),
                            0
                        )
                    )
                } else {
                    vibrator?.vibrate(
                        longArrayOf(0, 500, 300, 500),
                        0
                    )
                }
            }
        }
        // 次回比較するために、今回の距離を保存する
        previousDistance = distance



//            //将来的に消す
//            nowLongtitude += 0.0003
//            //nowLatitude += 0.0005

        //将来的に消す
//            if (isChecks) {
//                handler.postDelayed(this, 5000) //５秒後にまた呼び出す
//            }
//
    }

//    fun bearing(): Float {
//        // 目的地点
//        val startLocation = Location("start").apply {
//            latitude = targetLatitude!!
//            longitude = targetLongitude!!
//        }
//
//        // 現在地点
//        val endLocation = Location("end").apply {
//            latitude = nowLatitude!!
//            longitude = nowLongtitude!!
//        }
//
//        // bearingTo() で方位角を取得
//        val bearing = startLocation.bearingTo(endLocation) //% 180
//
//        return bearing
//    }


    // 開始　将来的に消す
//    fun startChecking() {
//        handler.post(checkRunnable)
//    }
//
//    // 停止
//    fun stopChecking() {
//        handler.removeCallbacks(checkRunnable)
//    }
}

