/* SPDX-License-Identifier: GPL-3.0-or-later
 * Manitou frame renderer for the Android 11 cm01 TRYX display.
 * Runs as the ADB shell user, without installing an APK or changing firmware.
 * Wire protocol: MTX1 greeting; signed big-endian int followed by JPEG bytes.
 * Positive length = frame, 0 = ping, -1 = hide, -2 = quit. Each succeeds with 1.
 */
package com.manitou.tryx;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.RandomAccessFile;
import java.lang.reflect.Method;
import java.nio.channels.FileLock;

public final class FrameServer {
    private static volatile long lastInput = System.nanoTime();
    private static Object control, surface, transaction;
    private static Class<?> controlClass, surfaceClass, transactionClass, canvasClass, bitmapClass;
    private static boolean visible;

    private static void createSurface() throws Exception {
        // Initialize the Android font manager before Canvas initializes Skia's
        // fallback typeface. app_process has no Activity/zygote preload phase.
        Class.forName("android.graphics.Typeface");
        controlClass = Class.forName("android.view.SurfaceControl");
        Class<?> builderClass = Class.forName("android.view.SurfaceControl$Builder");
        Object builder = builderClass.getConstructor().newInstance();
        builderClass.getMethod("setName", String.class).invoke(builder, "Manitou TRYX LCD");
        builderClass.getMethod("setBufferSize", int.class, int.class).invoke(builder, 2240, 1080);
        control = builderClass.getMethod("build").invoke(builder);
        surfaceClass = Class.forName("android.view.Surface");
        surface = surfaceClass.getConstructor().newInstance();
        surfaceClass.getMethod("copyFrom", controlClass).invoke(surface, control);
        canvasClass = Class.forName("android.graphics.Canvas");
        bitmapClass = Class.forName("android.graphics.Bitmap");
        transactionClass = Class.forName("android.view.SurfaceControl$Transaction");
        transaction = transactionClass.getConstructor().newInstance();
        transactionClass.getMethod("setLayerStack", controlClass, int.class)
                .invoke(transaction, control, 0);
        transactionClass.getMethod("setLayer", controlClass, int.class)
                .invoke(transaction, control, 2000000);
        transactionClass.getMethod("apply").invoke(transaction);
    }

    private static void setVisible(boolean value) throws Exception {
        if (visible != value) {
            transactionClass.getMethod(value ? "show" : "hide", controlClass)
                    .invoke(transaction, control);
            transactionClass.getMethod("apply").invoke(transaction);
            visible = value;
        }
    }

    private static void draw(byte[] data) throws Exception {
        Class<?> optionsClass = Class.forName("android.graphics.BitmapFactory$Options");
        Object options = optionsClass.getConstructor().newInstance();
        Method decode = Class.forName("android.graphics.BitmapFactory").getMethod(
                "decodeByteArray", byte[].class, int.class, int.class, optionsClass);
        optionsClass.getField("inJustDecodeBounds").setBoolean(options, true);
        decode.invoke(null, data, 0, data.length, options);
        if (optionsClass.getField("outWidth").getInt(options) != 2240
                || optionsClass.getField("outHeight").getInt(options) != 1080) {
            throw new IllegalArgumentException("Expected 2240x1080 frame");
        }
        optionsClass.getField("inJustDecodeBounds").setBoolean(options, false);
        Object bitmap = decode.invoke(null, data, 0, data.length, options);
        if (bitmap == null) throw new IllegalArgumentException("Invalid frame");
        try {
            Object canvas = surfaceClass.getMethod("lockCanvas", Class.forName("android.graphics.Rect"))
                    .invoke(surface, new Object[]{null});
            try {
                canvasClass.getMethod("drawBitmap", bitmapClass, float.class, float.class,
                        Class.forName("android.graphics.Paint")).invoke(canvas, bitmap, 0f, 0f, null);
            } finally {
                // Swap only after drawing the complete frame. The preceding
                // buffer remains on screen throughout transfer and decoding.
                surfaceClass.getMethod("unlockCanvasAndPost", canvasClass).invoke(surface, canvas);
            }
            setVisible(true);
        } finally {
            bitmapClass.getMethod("recycle").invoke(bitmap);
        }
    }

    private static void release() {
        try {
            if (control != null && transaction != null) {
                transactionClass.getMethod("remove", controlClass).invoke(transaction, control);
                transactionClass.getMethod("apply").invoke(transaction);
            }
            if (surface != null) surfaceClass.getMethod("release").invoke(surface);
            if (control != null) controlClass.getMethod("release").invoke(control);
            if (transaction != null) transactionClass.getMethod("close").invoke(transaction);
        } catch (Exception ignored) {
            // SurfaceFlinger also removes layers when the owning process dies.
        }
    }

    public static void main(String[] args) throws Exception {
        try (RandomAccessFile lockFile = new RandomAccessFile(
                "/data/local/tmp/manitou-tryx-renderer.lock", "rw");
             FileLock lock = lockFile.getChannel().tryLock()) {
            if (lock == null) throw new IllegalStateException("TRYX renderer already running");
            Thread watchdog = new Thread(new Runnable() {
                @Override public void run() {
                    try {
                        while (true) {
                            Thread.sleep(10000);
                            if (System.nanoTime() - lastInput > 90000000000L) System.exit(1);
                        }
                    } catch (InterruptedException ignored) { }
                }
            }, "manitou-watchdog");
            watchdog.setDaemon(true);
            watchdog.start();
            try {
                createSurface();
                DataInputStream input = new DataInputStream(System.in);
                DataOutputStream output = new DataOutputStream(System.out);
                output.writeBytes("MTX1");
                output.flush();
                while (true) {
                    int length;
                    try { length = input.readInt(); } catch (EOFException end) { break; }
                    if (length < -2 || length > 20 * 1024 * 1024) {
                        throw new IllegalArgumentException("Invalid frame length");
                    }
                    if (length > 0) {
                        byte[] data = new byte[length];
                        input.readFully(data);
                        draw(data);
                    } else if (length == -1) {
                        setVisible(false);
                    }
                    lastInput = System.nanoTime();
                    output.writeByte(1);
                    output.flush();
                    if (length == -2) break;
                }
            } finally {
                release();
            }
        }
    }
}
