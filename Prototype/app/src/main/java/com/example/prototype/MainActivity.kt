package com.example.prototype

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.util.Log
import androidx.activity.enableEdgeToEdge
import androidx.annotation.RequiresPermission
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import com.example.prototype.databinding.ActivityMainBinding
import androidx.appcompat.app.AlertDialog
import android.widget.Button
import android.widget.RadioButton
import androidx.core.content.ContextCompat
import android.os.Build
import android.widget.RadioGroup


class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding

    // 位置情報取得クラス
    private lateinit var locationGetter: LocationGetter

    // 一時停止標識検索クラス
    private lateinit var nearStopSign: NearStopSign
    private val handler = android.os.Handler(android.os.Looper.getMainLooper())

    // 監視状態管理用
    private var isMonitoring = false

    // 距離判定クラス
    private lateinit var distanceChecker: DistanceChecker

    // 初回起動判定クラス
    private lateinit var checker: FirstLaunchCheck
    @RequiresPermission(anyOf = [Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION])
    private val LOCATION_PERMISSION_REQUEST_CODE = 100
    // Activity生成時に呼ばれる
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        // 初回起動時のみ設定ダイアログを表示する
        checker = FirstLaunchCheck(this)
        //テスト中のみ(毎回初回起動判定にする)
        checker.resetFirstLaunch()
        // 初回起動の場合のみ通知方法を選択する
        if(checker.isFirstLaunch()) {
            val dialogView = layoutInflater.inflate(
                R.layout.dialog_mode_settings,
                null
            )
            val dialog =
                AlertDialog.Builder(this)
                    .setView(dialogView)
                    .create()
            dialog.setCancelable(false)
            val radioMusic = dialogView.findViewById<RadioButton>(
                R.id.radioMusic
            )

            val radioVibe = dialogView.findViewById<RadioButton>(
                R.id.radioVibe
            )

            val radioBoth = dialogView.findViewById<RadioButton>(
                R.id.radioBoth
            )

            val saveMode = checker.getAlertMode()

            when (saveMode) {
                FirstLaunchCheck.MODE_SOUND -> {
                    radioMusic.isChecked = true
                    radioVibe.isChecked = false
                    radioBoth.isChecked = false
                }

                FirstLaunchCheck.MODE_VIBRATION -> {
                    radioMusic.isChecked = false
                    radioVibe.isChecked = true
                    radioBoth.isChecked = false
                }

                FirstLaunchCheck.MODE_BOTH -> {
                    radioMusic.isChecked = false
                    radioVibe.isChecked = false
                    radioBoth.isChecked = true
                }
            }

            radioMusic.setOnClickListener {
                radioMusic.isChecked = true
                radioVibe.isChecked = false
                radioBoth.isChecked = false
            }

            radioVibe.setOnClickListener {
                radioMusic.isChecked = false
                radioVibe.isChecked = true
                radioBoth.isChecked = false
            }

            radioBoth.setOnClickListener {
                radioMusic.isChecked = false
                radioVibe.isChecked = false
                radioBoth.isChecked = true
            }
            val btnOk = dialogView.findViewById<Button>(
                R.id.btnOk
            )
            btnOk.setOnClickListener {
                //ラジオボタンの選択したもの取得(現在はログになってます)
                if (radioMusic.isChecked) {
                    checker.saveAlertMode(
                        FirstLaunchCheck.MODE_SOUND
                    )

                    Log.e("MODE", "音声モード")

                } else if (radioVibe.isChecked) {
                    checker.saveAlertMode(
                        FirstLaunchCheck.MODE_VIBRATION
                    )

                    Log.e("MODE", "バイブモード")

                } else if (radioBoth.isChecked) {
                    checker.saveAlertMode(
                        FirstLaunchCheck.MODE_BOTH
                    )

                    Log.e("MODE", "音声+バイブモード")
                }
                dialog.dismiss()
                checker.setFirstLaunchFinished()
            }
            dialog.show()
        }
        //通知欄表示
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                200
            )
        }

        locationGetter = LocationGetter(this)
        // DistanceChecker生成
        distanceChecker = DistanceChecker(this)
        nearStopSign = NearStopSign()
//        nearStopSign.setStopPoints(this)
        // 監視開始・停止ボタン
        binding.locationBtn.setOnClickListener() {
            // 監視状態を切り替える
            isMonitoring = !isMonitoring
            // 監視開始時の処理
            if (isMonitoring){
//                distanceChecker.stopSuccsesCount = 0
//                distanceChecker.stopPointsCount = 0
                if (ActivityCompat.checkSelfPermission(
                        this,
                        Manifest.permission.ACCESS_FINE_LOCATION
                    ) != PackageManager.PERMISSION_GRANTED && ActivityCompat.checkSelfPermission(
                        this,
                        Manifest.permission.ACCESS_COARSE_LOCATION
                    ) != PackageManager.PERMISSION_GRANTED
                ) {
                    Log.e("位置情報", "権限が許可されてないよ")
                    // 位置情報権限が無い場合は取得する
                    ActivityCompat.requestPermissions(
                        this,
                        arrayOf(
                            Manifest.permission.ACCESS_FINE_LOCATION,
                            Manifest.permission.ACCESS_COARSE_LOCATION
                        ),
                        LOCATION_PERMISSION_REQUEST_CODE
                    )
                    isMonitoring = false
                    return@setOnClickListener
                }
                //locationGetter.startLocationUpdate()
                binding.locationBtn.text = getString(R.string.btn_onLocation)
                binding.statusText.text = getString(R.string.status_on)
                /*lifecycleScope.launch {
                    while (isMonitoring) {
                        //近くの一時停止標識の配列の設定
                        nearStopSign.setStopPoints(this@MainActivity)
//                        Log.e("配列の確認","${nearStopSign.stopPoints}")
                        var nowLat = locationGetter.getLat()
                        var nowLong = locationGetter.getLong()
                        var nowBearing = locationGetter.getBearing()
                        if (nowLat == null || nowLong == null || nowBearing == null) {
                            Log.e("現在地なし","現在地なし")
                            delay(1000L)
                            continue
                        }
                        //一時停止の特定
                        val nearrestStop = nearStopSign.matchStopSing(
                            nowLat,
                            nowLong,
                            nowBearing,
                            nearStopSign.stopPoints
                        )

                        if (nearrestStop == null) {
                            Log.e("一時停止なし","一時停止なし")
                            delay(1000L)
                            continue
                        }

                        //最寄りの一時停止の値設定
                        distanceChecker.targetLatitude = nearrestStop["lat"] as Double
                        distanceChecker.targetLongitude = nearrestStop["long"] as Double
                        distanceChecker.isChecks = true // 地点特定完了通知
                        //現在地の値設定
                        distanceChecker.nowLatitude = locationGetter.getLat()
                        distanceChecker.nowLongtitude = locationGetter.getLong()
//                       速度受け渡し
                        distanceChecker.nowSpeed = locationGetter.getSpeed()
                        Log.e("test","${locationGetter.getSpeed()}")
//                        距離判定実行
                        distanceChecker.run()


                        delay(1000L)
//unrecoverably broken and will be disposed!
                        //ボタン押して少ししたら出るエラークラッシュする
                    }
                }*/
                // フォアグラウンドサービスを開始する （アプリを閉じても監視を継続できる）
                ContextCompat.startForegroundService(
                    this,
                    Intent(
                        this,
                        ForegroundService::class.java
                    )
                )
                // 距離判定実行開始
//                distanceChecker.startChecking()
            }else{
                //locationGetter.stopLocationonUpdate()
                // フォアグラウンドサービス停止
                stopService(
                    Intent(
                        this,
                        ForegroundService::class.java
                    )
                )
                binding.locationBtn.text = getString(R.string.btn_offLocation)
                binding.statusText.text = getString(R.string.status_off)
//                Log.e("test","停止した一時停止${distanceChecker.stopSuccsesCount}")
//                Log.e("test","接近した一時停止${distanceChecker.stopPointsCount}")
                // 距離判定停止
//                distanceChecker.stopChecking()
            }

        }


        // レポート画面へ遷移
        binding.btnReport.setOnClickListener {
            Log.e("btncheack","クリック")
            val intent = Intent(
                this,
                ReportActivity::class.java
            )
//            intent.putExtra("STOP_SUCCSES_COUNT",distanceChecker.stopSuccsesCount)
//            intent.putExtra("STOP_POINTS_COUNT",distanceChecker.stopPointsCount)
            startActivity(intent)
        }

        // 設定ダイアログ表示
        binding.btnSettings.setOnClickListener {
            val settingsView =layoutInflater.inflate(
                R.layout.activity_setting,
                null
            )
            val dialog = AlertDialog.Builder(this)
                .setView(settingsView)
                .create()

            //アラート用RadioGroup
            val radioAlertModeGroup = settingsView.findViewById<RadioGroup>(
                R.id. radioAlertModeGroup
            )

            //距離用RadioGroup
            val radioDistanceModeGroup = settingsView.findViewById<RadioGroup>(
                R.id. radioDistanceModeGroup
            )

            val Btnmap = settingsView.findViewById<Button>(R.id.btnReport)

            when (checker.getAlertMode()) {
                FirstLaunchCheck.MODE_SOUND -> {
                    radioAlertModeGroup.check(R.id.radioSound)
                }

                FirstLaunchCheck.MODE_VIBRATION -> {
                    radioAlertModeGroup.check(R.id.radioVibration)
                }

                FirstLaunchCheck.MODE_BOTH -> {
                    radioAlertModeGroup.check(R.id.radioBoth)
                }

                else -> {
                    radioAlertModeGroup.check(R.id.radioSound)
                }
            }

            when (checker.getDistanceMode()) {
                FirstLaunchCheck.MODE_NEAR -> {
                    radioDistanceModeGroup.check(R.id.radioNear)
                }

                FirstLaunchCheck.MODE_FAR -> {
                    radioDistanceModeGroup.check(R.id.radioFar)
                }

                else -> {
                    radioDistanceModeGroup.check(R.id.radioNear)
                }
            }

            radioAlertModeGroup.setOnCheckedChangeListener { _: RadioGroup, checkedId: Int ->
                when (checkedId) {
                    R.id.radioSound -> {
                        checker.saveAlertMode(
                            FirstLaunchCheck.MODE_SOUND
                        )
                    }

                    R.id.radioVibration -> {
                        checker.saveAlertMode(
                            FirstLaunchCheck.MODE_VIBRATION
                        )
                    }

                    R.id.radioBoth -> {
                        checker.saveAlertMode(
                            FirstLaunchCheck.MODE_BOTH
                        )
                    }
                }
            }


            radioDistanceModeGroup.setOnCheckedChangeListener { _: RadioGroup, checkedId: Int ->
                when (checkedId) {
                    R.id.radioNear -> {
                        checker.saveDistanceMode(
                            FirstLaunchCheck.MODE_NEAR
                        )
                    }

                    R.id.radioFar -> {
                        checker.saveDistanceMode(
                            FirstLaunchCheck.MODE_FAR
                        )
                    }
                }
            }

            Btnmap.setOnClickListener {
                val intent = Intent (
                    this,
                    MapActivity::class.java
                )

                startActivity(intent)
                dialog.dismiss()
            }
            dialog.show()
        }

    }
    // Activity終了時に位置情報取得を停止する
    override fun onDestroy() {
        super.onDestroy()
        locationGetter.stopLocationonUpdate()
        // 停止
//        distanceChecker.stopChecking()
    }
}

