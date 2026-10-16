package com.shuttle.helper;

import android.content.ClipData;
import android.graphics.Bitmap;
import android.os.IBinder;
import android.util.Base64;

import java.io.ByteArrayOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.reflect.Method;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;

/**
 * Shuttle phone helper. Runs on the phone WITHOUT installing an app:
 *
 *   adb push shuttle-helper.jar /data/local/tmp/
 *   adb exec-out CLASSPATH=/data/local/tmp/shuttle-helper.jar app_process /system/bin \
 *        com.shuttle.helper.Main screenshot 720 80 204800
 *
 * Commands:
 *   screenshot SHORT QUALITY MAXBYTES  -> stdout: "SHUTTLE1 <w> <h>\n" + JPEG (w,h = device pixels)
 *   clip BASE64_UTF8                   -> sets the clipboard (then the server sends KEYCODE_PASTE); prints OK
 *   version                            -> prints the helper version
 */
public final class Main {
    private static final String VERSION = "shuttle-helper 1";

    private Main() {}

    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            System.err.println("usage: screenshot SHORT QUALITY MAXBYTES | clip BASE64 | version");
            System.exit(2);
        }
        switch (args[0]) {
            case "screenshot":
                screenshot(Integer.parseInt(args[1]), Integer.parseInt(args[2]), Integer.parseInt(args[3]));
                break;
            case "clip":
                clip(args[1]);
                break;
            case "version":
                System.out.println(VERSION);
                break;
            default:
                System.err.println("unknown command " + args[0]);
                System.exit(2);
        }
    }

    private static byte[] readAll(InputStream in) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream(16 * 1024 * 1024);
        byte[] buf = new byte[256 * 1024];
        int n;
        while ((n = in.read(buf)) > 0) {
            out.write(buf, 0, n);
        }
        return out.toByteArray();
    }

    private static void screenshot(int shortSide, int quality, int maxBytes) throws Exception {
        // Raw screencap output: width, height, format (+ dataspace on Android 12+), then RGBA_8888.
        Process p = Runtime.getRuntime().exec(new String[] {"/system/bin/screencap"});
        byte[] raw = readAll(p.getInputStream());
        p.waitFor();
        if (raw.length < 16) {
            throw new IllegalStateException("screencap returned " + raw.length + " bytes");
        }
        ByteBuffer hdr = ByteBuffer.wrap(raw, 0, 16).order(ByteOrder.LITTLE_ENDIAN);
        int w = hdr.getInt();
        int h = hdr.getInt();
        int pixels = w * h * 4;
        int headerSize = raw.length - pixels;
        if (w <= 0 || h <= 0 || (headerSize != 12 && headerSize != 16)) {
            throw new IllegalStateException("unexpected screencap layout " + w + "x" + h + " len=" + raw.length);
        }
        Bitmap bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        bmp.copyPixelsFromBuffer(ByteBuffer.wrap(raw, headerSize, pixels));
        raw = null;

        double s = Math.min(1.0, (double) shortSide / Math.min(w, h));
        Bitmap scaled = bmp;
        if (s < 1.0) {
            scaled = Bitmap.createScaledBitmap(bmp, (int) Math.round(w * s), (int) Math.round(h * s), true);
            bmp.recycle();
        }
        byte[] jpeg = null;
        for (int attempt = 0; attempt < 8; attempt++) {
            for (int q = quality; q >= 35; q -= 10) {
                ByteArrayOutputStream out = new ByteArrayOutputStream(maxBytes);
                scaled.compress(Bitmap.CompressFormat.JPEG, q, out);
                jpeg = out.toByteArray();
                if (jpeg.length <= maxBytes) {
                    break;
                }
            }
            if (jpeg.length <= maxBytes || Math.min(scaled.getWidth(), scaled.getHeight()) <= 160) {
                break;
            }
            Bitmap smaller = Bitmap.createScaledBitmap(scaled, (int) (scaled.getWidth() * 0.85),
                    (int) (scaled.getHeight() * 0.85), true);
            scaled.recycle();
            scaled = smaller;
        }
        OutputStream os = new FileOutputStream(FileDescriptor.out);
        os.write(("SHUTTLE1 " + w + " " + h + "\n").getBytes(StandardCharsets.US_ASCII));
        os.write(jpeg);
        os.flush();
    }

    /** Same technique as scrcpy: talk to the hidden IClipboard service as the shell user. */
    private static void clip(String b64) throws Exception {
        String text = new String(Base64.decode(b64, Base64.NO_WRAP), StandardCharsets.UTF_8);
        Class<?> sm = Class.forName("android.os.ServiceManager");
        IBinder binder = (IBinder) sm.getMethod("getService", String.class).invoke(null, "clipboard");
        Class<?> stub = Class.forName("android.content.IClipboard$Stub");
        Object clipboard = stub.getMethod("asInterface", IBinder.class).invoke(null, binder);
        ClipData clip = ClipData.newPlainText("shuttle", text);
        Exception last = null;
        for (Method m : clipboard.getClass().getMethods()) {
            if (!m.getName().equals("setPrimaryClip")) {
                continue;
            }
            Class<?>[] types = m.getParameterTypes();
            Object[] values = new Object[types.length];
            int strings = 0;
            for (int i = 0; i < types.length; i++) {
                if (types[i] == ClipData.class) {
                    values[i] = clip;
                } else if (types[i] == String.class) {
                    values[i] = strings++ == 0 ? "com.android.shell" : null; // package, attributionTag
                } else if (types[i] == int.class) {
                    values[i] = 0; // userId, deviceId (DEVICE_ID_DEFAULT)
                } else {
                    values[i] = null;
                }
            }
            try {
                m.invoke(clipboard, values);
                System.out.println("OK");
                return;
            } catch (Exception e) {
                last = e;
            }
        }
        System.out.println("FAIL " + (last == null ? "no setPrimaryClip" : last.getCause() != null ? last.getCause() : last));
        System.exit(1);
    }
}
