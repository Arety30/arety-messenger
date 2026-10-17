package cloud.arety.messenger;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.CookieManager;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/** Arety Messenger: a full-screen window onto https://example.com with file attach and back-button support. */
public class MainActivity extends Activity {
    private static final String HOME = "https://example.com/";
    private static final int PICK_FILE = 1;
    private WebView web;
    private ValueCallback<Uri[]> filePicked;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        web = new WebView(this);
        setContentView(web);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);          // keeps you signed in
        s.setMediaPlaybackRequiresUserGesture(false);
        CookieManager.getInstance().setAcceptCookie(true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("example.com".equals(uri.getHost())) return false;
                startActivity(new Intent(Intent.ACTION_VIEW, uri));   // other links open in the browser
                return true;
            }

            @Override
            public void onReceivedError(WebView view, int code, String description, String url) {
                if (HOME.equals(url)) {
                    view.loadData("<body style='background:#0b141a;color:#e9edef;font-family:sans-serif;padding:32px'>"
                            + "<h2>Can't reach Arety Messenger</h2><p>Check your internet connection, and that the laptop "
                            + "hosting it is switched on.</p><p><a style='color:#00a884' href='" + HOME + "'>Try again</a></p></body>",
                            "text/html", "utf-8");
                }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (filePicked != null) filePicked.onReceiveValue(null);
                filePicked = callback;
                Intent pick = new Intent(Intent.ACTION_GET_CONTENT);
                pick.addCategory(Intent.CATEGORY_OPENABLE);
                pick.setType("*/*");
                pick.putExtra(Intent.EXTRA_MIME_TYPES, new String[] {"image/*", "video/*", "audio/*", "application/pdf"});
                try {
                    startActivityForResult(Intent.createChooser(pick, "Attach"), PICK_FILE);
                } catch (Exception e) {
                    filePicked = null;
                    return false;
                }
                return true;
            }
        });

        if (state == null) web.loadUrl(HOME); else web.restoreState(state);
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        if (request != PICK_FILE || filePicked == null) return;
        Uri[] picked = (result == RESULT_OK && data != null && data.getData() != null) ? new Uri[] {data.getData()} : null;
        filePicked.onReceiveValue(picked);
        filePicked = null;
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack(); else super.onBackPressed();
    }
}
