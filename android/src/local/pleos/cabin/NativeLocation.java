package local.pleos.cabin;

import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Handler;
import android.os.Looper;
import ai.pleos.playground.location.fused.FusedLocation;
import ai.pleos.playground.location.fused.listener.FusedLocationListener;
import ai.pleos.playground.location.fused.model.LocationData;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import org.json.JSONException;
import org.json.JSONObject;

/** Foreground-only vehicle location access through the public Pleos SDK. */
final class NativeLocation {
    static final String PERMISSION = "pleos.car.permission.FUSED_LOCATION";
    static final int PERMISSION_REQUEST = 13;
    // A single queue also serializes old and replacement Activity instances.
    private static final ExecutorService NETWORK = Executors.newSingleThreadExecutor();
    private static final AtomicLong EPOCH = new AtomicLong();
    private static final AtomicReference<Pending> LATEST = new AtomicReference<>();
    private static final AtomicBoolean DRAINING = new AtomicBoolean();
    private final Activity activity;
    private final Handler main = new Handler(Looper.getMainLooper());
    private FusedLocation helper;
    private FusedLocationListener listener;
    private boolean initialized;
    private boolean registered;
    private boolean active;
    private boolean destroyed;
    private boolean permissionRequested;
    private boolean permissionPending;
    private boolean liveCallbackSeen;
    private long generation;
    private long lastFixTimestamp;

    NativeLocation(Activity activity) { this.activity = activity; }

    void start() {
        if (destroyed || active) return;
        active = true;
        generation = EPOCH.incrementAndGet();
        lastFixTimestamp = 0;
        liveCallbackSeen = false;
        final long epoch = generation;
        try {
            FusedLocation candidate = new FusedLocation(activity.getApplicationContext());
            if (!candidate.isSupported()) {
                unavailable(epoch, "unsupported");
                return;
            }
            if (activity.checkSelfPermission(PERMISSION) != PackageManager.PERMISSION_GRANTED) {
                unavailable(epoch, "permission_required");
                if (!permissionRequested) {
                    permissionRequested = true;
                    permissionPending = true;
                    activity.requestPermissions(new String[]{PERMISSION}, PERMISSION_REQUEST);
                }
                return;
            }
            helper = candidate;
            listener = new FusedLocationListener() {
                @Override public void onChanged(LocationData value) {
                    main.post(() -> accept(epoch, value, false));
                }
                @Override public void onError(Exception error) {
                    main.post(() -> {
                        if (!current(epoch)) return;
                        liveCallbackSeen = true;
                        unavailable(epoch, error instanceof SecurityException ? "permission_required" : "error");
                    });
                }
            };
            unavailable(epoch, "unavailable");
            // Set before the call so a partial initialization is still released.
            initialized = true;
            candidate.initialize();
            candidate.registerFusedLocationCallback(listener);
            registered = true;
            candidate.getLastKnownLocation(new FusedLocationListener() {
                @Override public void onChanged(LocationData value) {
                    main.post(() -> accept(epoch, value, true));
                }
                @Override public void onError(Exception error) {
                    main.post(() -> {
                        // A late cache lookup failure must not discard a live fix.
                        if (current(epoch) && !liveCallbackSeen) unavailable(epoch,
                            error instanceof SecurityException ? "permission_required" : "error");
                    });
                }
            });
        } catch (SecurityException error) {
            permissionPending = false;
            releaseSdk();
            unavailable(epoch, "permission_required");
        } catch (RuntimeException | LinkageError error) {
            permissionPending = false;
            releaseSdk();
            unavailable(epoch, "error");
        }
    }

    void permissionResult() {
        permissionPending = false;
        if (destroyed || !active) return;
        stop();
        start();
    }

    private boolean current(long epoch) {
        return !destroyed && active && generation == epoch && EPOCH.get() == epoch;
    }

    private void accept(long epoch, LocationData data, boolean cached) {
        if (!current(epoch) || (cached && liveCallbackSeen)) return;
        if (!cached) liveCallbackSeen = true;
        if (data == null) {
            unavailable(epoch, "unavailable");
            return;
        }
        VehicleLocationFix fix = VehicleLocationFix.from(data.getLatitude(), data.getLongitude(),
            data.getConfidence(), data.getTimestampMillis(), System.currentTimeMillis());
        if (fix == null) {
            // A stale cached response cannot erase a newer sample already delivered.
            if (lastFixTimestamp == 0) unavailable(epoch, "unavailable");
            return;
        }
        if (fix.timestampMillis <= lastFixTimestamp) return;
        lastFixTimestamp = fix.timestampMillis;
        try {
            SimpleDateFormat utc = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US);
            utc.setTimeZone(TimeZone.getTimeZone("UTC"));
            JSONObject payload = new JSONObject().put("latitude", fix.latitude)
                .put("longitude", fix.longitude).put("source", "vehicle")
                .put("updated_at", utc.format(new Date(fix.timestampMillis)));
            if (fix.confidence != null) payload.put("confidence", fix.confidence);
            enqueue(epoch, payload);
        } catch (JSONException ignored) {
            unavailable(epoch, "error");
        }
    }

    void stop() {
        if (!active) return;
        active = false;
        releaseSdk();
        if (EPOCH.compareAndSet(generation, generation + 1)) {
            generation++;
            // The system permission dialog can pause this Activity. Keep the
            // actionable state until its result instead of calling it stopped.
            unavailable(generation, permissionPending ? "permission_required" : "stopped");
        }
    }

    void destroy() {
        stop();
        destroyed = true;
        main.removeCallbacksAndMessages(null);
    }

    private void releaseSdk() {
        FusedLocation previous = helper;
        FusedLocationListener previousListener = listener;
        helper = null;
        listener = null;
        if (previous != null) {
            if (registered && previousListener != null) {
                try { previous.unregisterFusedLocationCallback(previousListener); }
                catch (RuntimeException | LinkageError ignored) { }
            }
            if (initialized) {
                try { previous.release(); }
                catch (RuntimeException | LinkageError ignored) { }
            }
        }
        registered = false;
        initialized = false;
    }

    private static void unavailable(long epoch, String status) {
        try { enqueue(epoch, new JSONObject().put("available", false).put("status", status)); }
        catch (JSONException ignored) { }
    }

    private static void enqueue(long epoch, JSONObject payload) {
        if (EPOCH.get() != epoch) return;
        LATEST.set(new Pending(epoch, payload.toString()));
        drain();
    }

    private static void drain() {
        if (!DRAINING.compareAndSet(false, true)) return;
        NETWORK.execute(() -> {
            try {
                Pending item;
                while ((item = LATEST.getAndSet(null)) != null) {
                    if (EPOCH.get() == item.epoch) send(item);
                }
            } finally {
                DRAINING.set(false);
                if (LATEST.get() != null) drain();
            }
        });
    }

    private static void send(Pending pending) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection)new URL("http://127.0.0.1:8765/api/vehicle-location").openConnection();
            connection.setRequestMethod("POST");
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setConnectTimeout(3000);
            connection.setReadTimeout(3000);
            connection.setDoOutput(true);
            byte[] body = pending.payload.getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(body.length);
            if (EPOCH.get() != pending.epoch) return;
            try (OutputStream output = connection.getOutputStream()) { output.write(body); }
            int response = connection.getResponseCode();
            // Do not log request data, returned bodies, or exception messages.
            InputStream input = response < 400 ? connection.getInputStream() : connection.getErrorStream();
            if (input != null) input.close();
        } catch (Exception ignored) {
            // A new SDK sample is sent on the next update; never persist coordinates.
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private static final class Pending {
        final long epoch;
        final String payload;
        Pending(long epoch, String payload) { this.epoch = epoch; this.payload = payload; }
    }
}
