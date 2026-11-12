package local.pleos.cabin;

import java.io.File;
import java.net.URL;
import java.net.URLClassLoader;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;
import java.lang.reflect.InvocationTargetException;

/** Reproduce the runtime crash without vehicle data or an Android device. */
public final class KotlinResourcesTest {
    private static final class Resources extends URLClassLoader {
        private final URLClassLoader packaged;
        private final boolean hide;
        Resources(URL[] jars, File apk, boolean hide) throws Exception {
            super(jars, null);
            this.hide = hide;
            packaged = apk == null ? null : new URLClassLoader(new URL[]{apk.toURI().toURL()}, null);
        }
        private boolean runtimeResource(String name) {
            return name.endsWith(".kotlin_builtins") || name.startsWith("META-INF/services/");
        }
        @Override public URL getResource(String name) {
            if (runtimeResource(name)) {
                if (hide) return null;
                if (packaged != null) return packaged.getResource(name);
            }
            return super.getResource(name);
        }
        @Override public Enumeration<URL> getResources(String name) throws java.io.IOException {
            if (runtimeResource(name)) {
                if (hide) return Collections.enumeration(Collections.<URL>emptyList());
                if (packaged != null) return packaged.getResources(name);
            }
            return super.getResources(name);
        }
        @Override public void close() throws java.io.IOException {
            if (packaged != null) packaged.close();
            super.close();
        }
    }
    private static void resolveList(Resources loader) throws Exception {
        Class<?> reflection = loader.loadClass("kotlin.jvm.internal.Reflection");
        Object list = reflection.getMethod("getOrCreateKotlinClass", Class.class).invoke(null, List.class);
        Object parameters = loader.loadClass("kotlin.reflect.KClass").getMethod("getTypeParameters").invoke(list);
        if (((List<?>)parameters).size() != 1) throw new AssertionError("List must have one type parameter");
        loader.loadClass("kotlin.reflect.KClass").getMethod("getMembers").invoke(list);
    }
    public static void main(String[] args) throws Exception {
        List<URL> dependencies = new ArrayList<>();
        for (File file : new File(args[0]).listFiles()) if (file.getName().endsWith(".jar")) dependencies.add(file.toURI().toURL());
        URL[] jars = dependencies.toArray(new URL[0]);
        try (Resources full = new Resources(jars, null, false)) { resolveList(full); }
        boolean reproduced = false;
        try (Resources missing = new Resources(jars, null, true)) { resolveList(missing); }
        catch (InvocationTargetException error) {
            Throwable cause = error.getCause();
            while (cause.getCause() != null) cause = cause.getCause();
            String message = String.valueOf(cause.getMessage());
            if (!message.contains("Unresolved class: interface java.util.List")
                    && !message.contains("No BuiltInsLoader implementation was found")) throw error;
            reproduced = true;
        }
        if (!reproduced) throw new AssertionError("Expected original missing-resource crash");
        try (Resources packaged = new Resources(jars, new File(args[1]), false)) { resolveList(packaged); }
        System.out.println("Kotlin resources: missing-resource reflection failure reproduced; packaged APK resources resolve List reflection.");
    }
}
