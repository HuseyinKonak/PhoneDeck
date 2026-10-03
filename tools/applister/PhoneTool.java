package phonedeck;

import android.net.Uri;
import android.os.Binder;
import android.os.Bundle;
import android.os.IBinder;
import android.os.Process;
import android.telecom.PhoneAccountHandle;
import android.telecom.TelecomManager;
import android.util.Base64;

import java.lang.reflect.Array;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Runs on the phone through app_process and talks to the telephony services directly,
 * as the shell user (which holds SEND_SMS and CALL_PHONE).
 *
 * Usage (text arguments are URL-safe Base64 so no shell quoting is involved):
 *   PhoneTool sms SUB_ID NUMBER_B64 TEXT_B64 [dry]
 *   PhoneTool call SUB_ID NUMBER_B64 [dry]      (SUB_ID -1 = phone's default SIM)
 *   PhoneTool accounts
 *   PhoneTool clearnotifications [dry]
 *   PhoneTool torch SECONDS ON_MS OFF_MS        (blinks the camera flash, then exits)
 *
 * Prints one line: "OK ..." on success, "ERROR ..." otherwise. With "dry" nothing is
 * sent or dialled; the line describes what would be called.
 */
public final class PhoneTool {
    private static final String SHELL_PACKAGE = "com.android.shell";
    private static final String DRY_RUN = "dry";
    private static final int DEFAULT_SUBSCRIPTION = -1;
    private static final int UIDS_PER_USER = 100_000;
    private static final int TORCH_MAX_SECONDS = 120;
    private static final int TORCH_MIN_PHASE_MS = 50;
    private static final int TORCH_CAMERA_CANDIDATES = 4;
    private static final String ATTRIBUTION_STATE_CLASS = "android.content.AttributionSourceState";
    private static final int GSM_SINGLE_LIMIT = 160;
    private static final int GSM_PART_LIMIT = 153;
    private static final int UCS2_SINGLE_LIMIT = 70;
    private static final int UCS2_PART_LIMIT = 67;
    /** GSM 03.38 default alphabet; anything else forces the 16-bit encoding. */
    private static final String GSM_BASIC =
            "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
            + "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà";
    /** Characters from the GSM extension table; each one takes two septets. */
    private static final String GSM_EXTENDED = "^{}\\[~]|€";

    private PhoneTool() {
    }

    public static void main(String[] args) {
        int exitCode = 0;
        try {
            System.out.println("OK " + run(args));
        } catch (InvocationTargetException e) {
            System.out.println("ERROR " + describe(e.getCause()));
            exitCode = 1;
        } catch (Exception e) {
            System.out.println("ERROR " + describe(e));
            exitCode = 1;
        }
        System.out.flush();
        System.exit(exitCode);
    }

    private static String run(String[] args) throws Exception {
        if (args.length == 0) {
            throw new IllegalArgumentException("missing command");
        }
        boolean isDryRun = DRY_RUN.equals(args[args.length - 1]);
        switch (args[0]) {
            case "sms":
                return sendSms(Integer.parseInt(args[1]), decode(args[2]), decode(args[3]), isDryRun);
            case "call":
                return placeCall(Integer.parseInt(args[1]), decode(args[2]), isDryRun);
            case "accounts":
                return describeAccounts(callCapableAccounts());
            case "clearnotifications":
                return clearNotifications(isDryRun);
            case "torch":
                return blinkTorch(Integer.parseInt(args[1]), Integer.parseInt(args[2]), Integer.parseInt(args[3]));
            default:
                throw new IllegalArgumentException("unknown command " + args[0]);
        }
    }

    private static String decode(String value) {
        return new String(Base64.decode(value, Base64.URL_SAFE), StandardCharsets.UTF_8);
    }

    private static String describe(Throwable error) {
        return error == null ? "unknown" : error.getClass().getSimpleName() + ": " + error.getMessage();
    }

    // --- binder plumbing ------------------------------------------------------
    private static Object service(String name, String interfaceName) throws Exception {
        Class<?> serviceManager = Class.forName("android.os.ServiceManager");
        IBinder binder = (IBinder) serviceManager.getMethod("getService", String.class).invoke(null, name);
        if (binder == null) {
            throw new IllegalStateException("service not found: " + name);
        }
        return Class.forName(interfaceName + "$Stub").getMethod("asInterface", IBinder.class).invoke(null, binder);
    }

    private static Method method(Object target, String name) throws NoSuchMethodException {
        for (Method candidate : target.getClass().getMethods()) {
            if (candidate.getName().equals(name)) {
                return candidate;
            }
        }
        throw new NoSuchMethodException(name);
    }

    /**
     * Builds an argument list for a hidden binder method whose exact signature differs
     * between Android versions: strings, the list and the first int are taken from the
     * caller; every other parameter gets a neutral default.
     */
    private static Object[] arguments(Method method, int firstInt, String[] strings, List<String> list) {
        Class<?>[] types = method.getParameterTypes();
        Object[] values = new Object[types.length];
        int stringIndex = 0;
        boolean isIntUsed = false;
        boolean isListUsed = false;
        for (int i = 0; i < types.length; i++) {
            if (types[i] == String.class) {
                values[i] = stringIndex < strings.length ? strings[stringIndex++] : null;
            } else if (types[i] == int.class) {
                values[i] = isIntUsed ? 0 : firstInt;
                isIntUsed = true;
            } else if (types[i] == boolean.class) {
                values[i] = true; // persistMessageForNonDefaultSmsApp / includeDisabledAccounts
            } else if (types[i] == long.class) {
                values[i] = 0L;
            } else if (types[i] == List.class && !isListUsed) {
                values[i] = list;
                isListUsed = true;
            } else {
                values[i] = null; // PendingIntent and friends
            }
        }
        return values;
    }

    private static int countStrings(Method method) {
        int count = 0;
        for (Class<?> type : method.getParameterTypes()) {
            if (type == String.class) {
                count++;
            }
        }
        return count;
    }

    // --- SMS ------------------------------------------------------------------
    private static String sendSms(int subId, String number, String text, boolean isDryRun) throws Exception {
        if (number.isEmpty() || text.isEmpty()) {
            throw new IllegalArgumentException("number and text are required");
        }
        Object sms = service("isms", "com.android.internal.telephony.ISms");
        ArrayList<String> parts = splitMessage(text);
        boolean isMultipart = parts.size() > 1;
        Method method = method(sms, isMultipart ? "sendMultipartTextForSubscriber" : "sendTextForSubscriber");
        // Newer Android versions carry an attribution tag after the calling package.
        boolean hasAttributionTag = countStrings(method) >= (isMultipart ? 4 : 5);
        String[] strings;
        if (isMultipart) {
            strings = hasAttributionTag
                    ? new String[] {SHELL_PACKAGE, null, number, null}
                    : new String[] {SHELL_PACKAGE, number, null};
        } else {
            strings = hasAttributionTag
                    ? new String[] {SHELL_PACKAGE, null, number, null, text}
                    : new String[] {SHELL_PACKAGE, number, null, text};
        }
        Object[] values = arguments(method, subId, strings, parts);
        String summary = method.getName() + " sub=" + subId + " parts=" + parts.size()
                + " params=" + method.getParameterTypes().length;
        if (!isDryRun) {
            method.invoke(sms, values);
        }
        return (isDryRun ? "dry " : "sent ") + summary;
    }

    /**
     * Splits a message into parts that each fit one SMS. The platform splitter needs an
     * application context, so this uses the standard limits directly: 160/153 septets
     * for GSM 7-bit text and 70/67 characters once any other character is present.
     */
    static ArrayList<String> splitMessage(String text) {
        boolean isGsm = true;
        for (int i = 0; i < text.length() && isGsm; i++) {
            char c = text.charAt(i);
            isGsm = GSM_BASIC.indexOf(c) >= 0 || GSM_EXTENDED.indexOf(c) >= 0;
        }
        int single = isGsm ? GSM_SINGLE_LIMIT : UCS2_SINGLE_LIMIT;
        int multi = isGsm ? GSM_PART_LIMIT : UCS2_PART_LIMIT;
        ArrayList<String> parts = new ArrayList<>();
        if (cost(text, isGsm) <= single) {
            parts.add(text);
            return parts;
        }
        StringBuilder part = new StringBuilder();
        int used = 0;
        for (int i = 0; i < text.length(); i++) {
            String unit = unitAt(text, i);
            int unitCost = cost(unit, isGsm);
            if (used + unitCost > multi) {
                parts.add(part.toString());
                part.setLength(0);
                used = 0;
            }
            part.append(unit);
            used += unitCost;
            i += unit.length() - 1;
        }
        if (part.length() > 0) {
            parts.add(part.toString());
        }
        return parts;
    }

    /** One character, or a surrogate pair kept together so emoji are never cut in half. */
    private static String unitAt(String text, int index) {
        boolean isPair = Character.isHighSurrogate(text.charAt(index)) && index + 1 < text.length();
        return text.substring(index, index + (isPair ? 2 : 1));
    }

    private static int cost(String text, boolean isGsm) {
        if (!isGsm) {
            return text.length();
        }
        int septets = 0;
        for (int i = 0; i < text.length(); i++) {
            septets += GSM_EXTENDED.indexOf(text.charAt(i)) >= 0 ? 2 : 1;
        }
        return septets;
    }

    // --- notifications --------------------------------------------------------
    /** Dismisses every clearable notification, like the "clear all" button of the panel. */
    private static String clearNotifications(boolean isDryRun) throws Exception {
        Object statusBar = service("statusbar", "com.android.internal.statusbar.IStatusBarService");
        Method method = method(statusBar, "onClearAllNotifications");
        int userId = Process.myUid() / UIDS_PER_USER;
        if (!isDryRun) {
            method.invoke(statusBar, arguments(method, userId, new String[0], null));
        }
        return (isDryRun ? "dry " : "cleared ") + "onClearAllNotifications user=" + userId
                + " params=" + method.getParameterTypes().length;
    }

    // --- torch ----------------------------------------------------------------
    /**
     * Blinks the camera flash for the given time, then switches it off. The camera service
     * ties the torch to this process, so killing the process also turns the light off.
     */
    private static String blinkTorch(int seconds, int onMs, int offMs) throws Exception {
        if (seconds < 1 || seconds > TORCH_MAX_SECONDS || Math.min(onMs, offMs) < TORCH_MIN_PHASE_MS) {
            throw new IllegalArgumentException("torch timing out of range");
        }
        Object camera = service("media.camera", "android.hardware.ICameraService");
        Method method = method(camera, "setTorchMode");
        IBinder client = new Binder();
        String cameraId = switchOnFirstTorch(camera, method, client);
        // Reported right away: the caller reads this line while the light keeps blinking.
        System.out.println("OK torch camera=" + cameraId);
        System.out.flush();
        long end = System.currentTimeMillis() + seconds * 1000L;
        boolean isOn = true;
        try {
            while (System.currentTimeMillis() < end) {
                Thread.sleep(isOn ? onMs : offMs);
                isOn = !isOn;
                method.invoke(camera, torchArguments(method, cameraId, isOn, client));
            }
        } finally {
            method.invoke(camera, torchArguments(method, cameraId, false, client));
        }
        return "torch finished camera=" + cameraId;
    }

    /** Front cameras usually have no flash, so the ids are tried in order until one lights up. */
    private static String switchOnFirstTorch(Object camera, Method method, IBinder client) throws Exception {
        Throwable lastError = null;
        for (int id = 0; id < TORCH_CAMERA_CANDIDATES; id++) {
            String cameraId = String.valueOf(id);
            try {
                method.invoke(camera, torchArguments(method, cameraId, true, client));
                return cameraId;
            } catch (InvocationTargetException e) {
                lastError = e.getCause();
            }
        }
        throw new IllegalStateException("no camera flash available: " + describe(lastError)
                + " signature=" + Arrays.toString(method.getParameterTypes()));
    }

    /**
     * The signature differs by version: Android 15 added a device id and policy (both 0 by
     * default) and Android 16 replaced the device id with the caller's attribution.
     */
    private static Object[] torchArguments(Method method, String cameraId, boolean isOn, IBinder client)
            throws Exception {
        Class<?>[] types = method.getParameterTypes();
        Object[] values = new Object[types.length];
        for (int i = 0; i < types.length; i++) {
            if (types[i] == String.class) {
                values[i] = cameraId;
            } else if (types[i] == boolean.class) {
                values[i] = isOn;
            } else if (types[i] == IBinder.class) {
                values[i] = client;
            } else if (types[i] == int.class) {
                values[i] = 0;
            } else if (types[i].getName().equals(ATTRIBUTION_STATE_CLASS)) {
                values[i] = shellAttribution(types[i], client);
            }
        }
        return values;
    }

    /** Describes this process (the shell user) to services that ask who is calling. */
    private static Object shellAttribution(Class<?> type, IBinder token) throws Exception {
        Object state = type.getDeclaredConstructor().newInstance();
        type.getField("uid").setInt(state, Process.myUid());
        type.getField("pid").setInt(state, Process.myPid());
        type.getField("deviceId").setInt(state, 0);
        type.getField("packageName").set(state, SHELL_PACKAGE);
        type.getField("token").set(state, token);
        type.getField("renouncedPermissions").set(state, new String[0]);
        type.getField("next").set(state, Array.newInstance(type, 0));
        return state;
    }

    // --- calls ----------------------------------------------------------------
    @SuppressWarnings("unchecked")
    private static List<PhoneAccountHandle> callCapableAccounts() throws Exception {
        Object telecom = service("telecom", "com.android.internal.telecom.ITelecomService");
        Method method = method(telecom, "getCallCapablePhoneAccounts");
        Object result = method.invoke(telecom, arguments(method, 0, new String[] {SHELL_PACKAGE, null}, null));
        // The service returns either a List or a ParceledListSlice depending on the version.
        if (result instanceof List) {
            return (List<PhoneAccountHandle>) result;
        }
        return (List<PhoneAccountHandle>) result.getClass().getMethod("getList").invoke(result);
    }

    private static String describeAccounts(List<PhoneAccountHandle> accounts) {
        StringBuilder text = new StringBuilder("accounts=" + accounts.size());
        for (PhoneAccountHandle account : accounts) {
            text.append(" [").append(account.getComponentName().getShortClassName())
                    .append(" idLength=").append(account.getId().length()).append(']');
        }
        return text.toString();
    }

    /** Telephony accounts are identified by the subscription id (or, on old versions, the ICC id). */
    private static PhoneAccountHandle accountFor(int subId, List<PhoneAccountHandle> accounts) {
        String wanted = String.valueOf(subId);
        for (PhoneAccountHandle account : accounts) {
            if (wanted.equals(account.getId())) {
                return account;
            }
        }
        return null;
    }

    private static String placeCall(int subId, String number, boolean isDryRun) throws Exception {
        if (number.isEmpty()) {
            throw new IllegalArgumentException("number is required");
        }
        Bundle extras = new Bundle();
        String sim = "default";
        if (subId != DEFAULT_SUBSCRIPTION) {
            PhoneAccountHandle account = accountFor(subId, callCapableAccounts());
            if (account == null) {
                throw new IllegalStateException("no phone account for subscription " + subId);
            }
            extras.putParcelable(TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE, account);
            sim = "sub " + subId;
        }
        Object telecom = service("telecom", "com.android.internal.telecom.ITelecomService");
        Method method = method(telecom, "placeCall");
        Class<?>[] types = method.getParameterTypes();
        Object[] values = arguments(method, 0, new String[] {SHELL_PACKAGE, null}, null);
        for (int i = 0; i < types.length; i++) {
            if (types[i] == Uri.class) {
                values[i] = Uri.fromParts("tel", number, null);
            } else if (types[i] == Bundle.class) {
                values[i] = extras;
            }
        }
        if (!isDryRun) {
            method.invoke(telecom, values);
        }
        return (isDryRun ? "dry " : "dialing ") + "placeCall sim=" + sim + " params=" + types.length;
    }
}
