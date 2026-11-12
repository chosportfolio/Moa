package local.pleos.cabin;

import android.content.Context;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Documented Gleo function actions and callback broadcasts, separate from STT. */
final class NativeGleo {
    interface Listener { void onGleoAction(String json); }

    static final String START = "START_CABIN_CONVERSATION";
    static final String RECOMMEND = "RECOMMEND_CABIN_PLACES";
    static final String SAVED = "SHOW_SAVED_CABIN_PLACES";
    private static final String GLEO_PACKAGE = "ai.umos.ambientai";
    private static final String RESPONSE_ACTION = "ai.pleos.playground.intent.action.APP_ACTION_RESPONSE";
    // Gleo cancels an action after seven seconds. Respond before that deadline.
    private static final long RESPONSE_TIMEOUT_MS = 6000;

    private final Context context;
    private final Listener listener;
    private final Handler main = new Handler(Looper.getMainLooper());
    private Command pending;
    private Runnable deadline;
    private boolean ready;
    private boolean closed;

    NativeGleo(Context context, Listener listener) {
        this.context = context.getApplicationContext();
        this.listener = listener;
    }

    synchronized void accept(Intent intent) {
        if (closed || intent == null || actionType(intent.getAction()) == null) return;
        String action = intent.getAction();
        // The intent has been consumed. Activity recreation must not replay it.
        intent.setAction(Intent.ACTION_MAIN);
        if (pending != null) {
            respond(action, "fail", "The app is already handling another voice request.", null);
            return;
        }
        pending = new Command(UUID.randomUUID().toString(), action, SystemClock.elapsedRealtime());
        deadline = () -> expire();
        main.postDelayed(deadline, RESPONSE_TIMEOUT_MS);
        dispatch();
    }

    synchronized void setReady(boolean value) {
        ready = value;
        if (ready) dispatch();
    }

    private void dispatch() {
        if (closed || !ready || pending == null || !pending.take(SystemClock.elapsedRealtime())) return;
        try {
            JSONObject event = new JSONObject();
            event.put("id", pending.id);
            event.put("action", actionType(pending.action));
            event.put("parameters", new JSONObject());
            event.put("expiresAt", System.currentTimeMillis()
                + Math.max(0, RESPONSE_TIMEOUT_MS - (SystemClock.elapsedRealtime() - pending.receivedAt)));
            listener.onGleoAction(event.toString());
        } catch (JSONException | RuntimeException error) {
            finish("fail", "The app could not open the requested screen.", null);
        }
    }

    /** Called by the trusted WebView. True means the matching callback was sent. */
    synchronized boolean complete(String id, String status, String message, String data) {
        if (closed || !ready || pending == null || !pending.id.equals(id)) return false;
        if (!pending.fresh(SystemClock.elapsedRealtime())) {
            expire();
            return false;
        }
        if (!validStatus(status) || message == null || message.trim().isEmpty() || message.length() > 512) {
            finish("fail", "The app could not confirm the requested action.", null);
            return false;
        }
        String safeData = null;
        if (data != null && !data.trim().isEmpty()) {
            try {
                if (data.length() > 8192) throw new JSONException("Callback data too large");
                safeData = new JSONArray(data).toString();
            } catch (JSONException error) {
                finish("fail", "The app could not confirm the requested result.", null);
                return false;
            }
        }
        return finish(status, message, safeData);
    }

    private synchronized void expire() {
        if (pending != null) finish("fail", "The app could not confirm the action in time. Check the app screen.", null);
    }

    private boolean finish(String status, String message, String data) {
        Command command = pending;
        pending = null;
        if (deadline != null) main.removeCallbacks(deadline);
        deadline = null;
        return command != null && respond(command.action, status, message, data);
    }

    private boolean respond(String action, String status, String message, String data) {
        try {
            JSONObject response = new JSONObject();
            response.put("functionName", action);
            response.put("status", status);
            response.put("message", message);
            // Starting cabin capture must not record Gleo reading its own reply.
            response.put("mode", START.equals(action) && "success".equals(status) ? "silent" : "voice");
            if (data != null) response.put("data", data);
            Intent callback = new Intent(RESPONSE_ACTION);
            callback.setPackage(GLEO_PACKAGE);
            callback.putExtra("packageName", context.getPackageName());
            callback.putExtra("response", response.toString());
            context.sendBroadcast(callback);
            return true;
        } catch (JSONException | RuntimeException error) {
            return false;
        }
    }

    synchronized void close() {
        if (pending != null) finish("fail", "The app closed before the action was confirmed.", null);
        closed = true;
        ready = false;
    }

    static String actionType(String action) {
        if (START.equals(action)) return "start_conversation";
        if (RECOMMEND.equals(action)) return "recommend_places";
        if (SAVED.equals(action)) return "show_saved";
        return null;
    }

    static boolean validStatus(String status) {
        return "success".equals(status) || "asking".equals(status) || "fail".equals(status);
    }

    static final class Command {
        final String id;
        final String action;
        final long receivedAt;
        private boolean delivered;

        Command(String id, String action, long receivedAt) {
            this.id = id;
            this.action = action;
            this.receivedAt = receivedAt;
        }

        boolean fresh(long now) {
            return now >= receivedAt && now - receivedAt < RESPONSE_TIMEOUT_MS;
        }

        boolean take(long now) {
            if (delivered || !fresh(now)) return false;
            delivered = true;
            return true;
        }
    }
}
