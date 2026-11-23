package dev.quartz.app;

import android.content.Intent;
import android.os.Bundle;
import android.webkit.WebView;

import com.getcapacitor.BridgeActivity;

/**
 * The shell's only Android code with a job of its own: take a share from another app
 * and hand the text to the web layer, and let the WebView's own history handle Back.
 */
public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(QuartzNativePlugin.class);
        super.onCreate(savedInstanceState);
        handleShareIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleShareIntent(intent);
        // The app was already running when the share arrived, so the web layer needs
        // a nudge to look for it.
        WebView webView = getBridge() == null ? null : getBridge().getWebView();
        if (webView != null) {
            webView.post(() -> webView.evaluateJavascript(
                "window.dispatchEvent(new Event('quartz:shared-text'))", null
            ));
        }
    }

    private void handleShareIntent(Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        if (!Intent.ACTION_SEND.equals(action) && !Intent.ACTION_VIEW.equals(action)) return;
        String type = intent.getType();
        if (type != null && type.startsWith("text/")) {
            CharSequence text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT);
            if (text != null && text.length() > 0) {
                QuartzNativePlugin.setSharedText(text.toString());
            }
            return;
        }
        // Anything else (a photo, a PDF) arrives as a content URI. Passing the URI on
        // lets the web layer decide; fetching it is not this class's business.
        android.net.Uri data = intent.getData();
        if (data != null) {
            QuartzNativePlugin.setSharedText(data.toString());
        }
    }
}
