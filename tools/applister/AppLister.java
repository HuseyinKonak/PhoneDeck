package phonedeck;

import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.drawable.Drawable;
import android.os.Looper;

import java.io.File;
import java.io.FileOutputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;

/**
 * Runs on the phone through app_process and prints one line per installed app:
 * {@code APP<TAB>package<TAB>isSystem(0|1)<TAB>label}. Launcher icons are written
 * as PNG files into the output directory, skipping the ones already there.
 *
 * Usage: AppLister OUTPUT_DIR ICON_SIZE user|all
 */
public final class AppLister {
    private static final String LINE_PREFIX = "APP";
    private static final int PNG_QUALITY = 100;

    private AppLister() {
    }

    public static void main(String[] args) throws Exception {
        File outputDir = new File(args[0]);
        int iconSize = Integer.parseInt(args[1]);
        boolean includeSystem = "all".equals(args[2]);
        if (!outputDir.isDirectory() && !outputDir.mkdirs()) {
            throw new IllegalStateException("Cannot create " + outputDir);
        }

        Looper.prepareMainLooper();
        PackageManager packageManager = systemContext().getPackageManager();
        for (ApplicationInfo info : packageManager.getInstalledApplications(0)) {
            boolean isSystem = (info.flags & ApplicationInfo.FLAG_SYSTEM) != 0;
            if (isSystem && !includeSystem) {
                continue;
            }
            writeIconIfMissing(packageManager, info, outputDir, iconSize);
            System.out.println(LINE_PREFIX + "\t" + info.packageName + "\t" + (isSystem ? "1" : "0")
                    + "\t" + readLabel(packageManager, info));
        }
        System.out.flush();
        // ActivityThread leaves non-daemon threads behind; exit explicitly.
        System.exit(0);
    }

    /** Builds a system Context without a real application (same trick scrcpy uses). */
    private static Context systemContext() throws Exception {
        Class<?> activityThreadClass = Class.forName("android.app.ActivityThread");
        Constructor<?> constructor = activityThreadClass.getDeclaredConstructor();
        constructor.setAccessible(true);
        Object activityThread = constructor.newInstance();
        Field current = activityThreadClass.getDeclaredField("sCurrentActivityThread");
        current.setAccessible(true);
        current.set(null, activityThread);
        attachConfigurationController(activityThreadClass, activityThread);
        return (Context) activityThreadClass.getDeclaredMethod("getSystemContext").invoke(activityThread);
    }

    /** Android 12+ reads resources through this controller; a bare ActivityThread has none. */
    private static void attachConfigurationController(Class<?> activityThreadClass, Object activityThread)
            throws ReflectiveOperationException {
        Class<?> controllerClass;
        Class<?> internalClass;
        try {
            controllerClass = Class.forName("android.app.ConfigurationController");
            internalClass = Class.forName("android.app.ActivityThreadInternal");
        } catch (ClassNotFoundException e) {
            return; // Older Android: not needed.
        }
        Constructor<?> constructor = controllerClass.getDeclaredConstructor(internalClass);
        constructor.setAccessible(true);
        Field field = activityThreadClass.getDeclaredField("mConfigurationController");
        field.setAccessible(true);
        field.set(activityThread, constructor.newInstance(activityThread));
    }

    /** Prefers the launcher activity's label (what the home screen shows) over the app label. */
    private static String readLabel(PackageManager packageManager, ApplicationInfo info) {
        try {
            Intent launch = packageManager.getLaunchIntentForPackage(info.packageName);
            ResolveInfo launcher = launch == null ? null : packageManager.resolveActivity(launch, 0);
            String label = launcher == null ? "" : clean(launcher.loadLabel(packageManager));
            if (!isUsable(label)) {
                label = clean(info.loadLabel(packageManager));
            }
            return isUsable(label) ? label : info.packageName;
        } catch (RuntimeException e) {
            System.err.println("label failed for " + info.packageName + ": " + e);
            return info.packageName;
        }
    }

    private static String clean(CharSequence label) {
        return label == null ? "" : label.toString().replaceAll("\\s+", " ").trim();
    }

    /** Some apps declare an unresolved resource reference such as "@string/name" as label. */
    private static boolean isUsable(String label) {
        return !label.isEmpty() && !label.startsWith("@");
    }

    private static void writeIconIfMissing(PackageManager packageManager, ApplicationInfo info,
            File outputDir, int size) {
        File target = new File(outputDir, info.packageName + ".png");
        if (target.exists()) {
            return;
        }
        try {
            Drawable drawable = info.loadUnbadgedIcon(packageManager);
            Bitmap bitmap = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
            drawable.setBounds(0, 0, size, size);
            drawable.draw(new Canvas(bitmap));
            try (FileOutputStream stream = new FileOutputStream(target)) {
                bitmap.compress(Bitmap.CompressFormat.PNG, PNG_QUALITY, stream);
            }
            bitmap.recycle();
        } catch (Exception e) {
            System.err.println("icon failed for " + info.packageName + ": " + e);
            if (target.exists() && !target.delete()) {
                System.err.println("could not remove partial " + target);
            }
        }
    }
}
