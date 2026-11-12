package local.pleos.cabin;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

/** Keeps only the user's active native-navigation callback session alive. No audio or GPS capture. */
public final class NavigationTrackingService extends Service {
    private static final String CHANNEL = "moa-navigation-tracking";
    private static final int NOTIFICATION = 2202;

    static void start(Context context) {
        context.startForegroundService(new Intent(context, NavigationTrackingService.class));
    }

    static void stop(Context context) {
        context.stopService(new Intent(context, NavigationTrackingService.class));
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        NativeNavigation navigation = NativeNavigation.get(getApplicationContext());
        // A request may have been cancelled before Android delivered this start.
        // A non-sticky restart must never invent or resume an old tracking session.
        if (!navigation.backgroundTrackingRequired()) {
            stopSelf(startId);
            return START_NOT_STICKY;
        }
        try {
            NotificationManager manager = getSystemService(NotificationManager.class);
            manager.createNotificationChannel(new NotificationChannel(
                CHANNEL, "길안내 연결", NotificationManager.IMPORTANCE_LOW));
            Intent open = new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            PendingIntent content = PendingIntent.getActivity(this, 2202, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            int icon = getResources().getIdentifier("ic_moa", "drawable", getPackageName());
            Notification notification = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(icon == 0 ? android.R.drawable.ic_dialog_map : icon)
                .setContentTitle("모아 · 길안내 연결 중")
                .setContentText("순정 내비의 안내와 도착 상태를 확인하고 있어요.")
                .setContentIntent(content)
                .setCategory(Notification.CATEGORY_SERVICE)
                .setOngoing(true).setOnlyAlertOnce(true).build();
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTIFICATION, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else {
                startForeground(NOTIFICATION, notification);
            }
            Log.i("CabinNavigation", "Foreground navigation tracking active");
        } catch (RuntimeException failure) {
            Log.e("CabinNavigation", "Foreground navigation tracking failed", failure);
            navigation.backgroundTrackingFailed();
            stopSelf(startId);
        }
        return START_NOT_STICKY;
    }

    @Override public void onTaskRemoved(Intent rootIntent) {
        NativeNavigation navigation = NativeNavigation.get(getApplicationContext());
        navigation.finishTracking(navigation.backgroundTrackingOwner());
        stopSelf();
    }

    @Override public void onDestroy() {
        stopForeground(STOP_FOREGROUND_REMOVE);
        Log.i("CabinNavigation", "Foreground navigation tracking stopped");
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
