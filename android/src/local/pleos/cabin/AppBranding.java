package local.pleos.cabin;

import android.app.Activity;
import android.app.ActivityManager;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.drawable.Drawable;
import android.os.Build;
import android.util.Log;

/** Supplies the current identity to task surfaces as well as the launcher. */
final class AppBranding {
    private static Bitmap taskIcon;

    private AppBranding() {}

    static void updateTask(Activity activity) {
        try {
            activity.setTitle("모아");
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                int id = activity.getResources().getIdentifier("ic_moa", "mipmap", activity.getPackageName());
                if (id == 0) throw new IllegalStateException("Moa launcher icon is missing");
                // A resource ID lets taskbars load the packaged adaptive icon.
                // Bitmap-only task descriptions do not provide iconResource.
                activity.setTaskDescription(new ActivityManager.TaskDescription.Builder()
                    .setLabel("모아")
                    .setIcon(id)
                    .setPrimaryColor(Color.rgb(20, 52, 74))
                    .build());
                return;
            }
            if (taskIcon == null) {
                int id = activity.getResources().getIdentifier("ic_moa", "drawable", activity.getPackageName());
                if (id == 0) throw new IllegalStateException("Moa task icon is missing");
                Drawable drawable = activity.getDrawable(id);
                if (drawable == null) throw new IllegalStateException("Moa task icon cannot be loaded");
                Bitmap bitmap = Bitmap.createBitmap(128, 128, Bitmap.Config.ARGB_8888);
                drawable.setBounds(0, 0, 128, 128);
                drawable.draw(new Canvas(bitmap));
                taskIcon = bitmap;
            }
            // Older Android releases do not expose the resource-based builder.
            activity.setTaskDescription(new ActivityManager.TaskDescription(
                "모아", taskIcon, Color.rgb(20, 52, 74)));
        } catch (RuntimeException error) {
            Log.w("CabinBranding", "Task icon refresh failed", error);
        }
    }
}
