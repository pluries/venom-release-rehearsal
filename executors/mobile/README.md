# Venom - Executor Mobile

Step to drive a native or hybrid Android or iOS app from a venom suite: install an app, find elements, tap, type, read text, scroll,
take a screenshot, open a deep link, switch into a web view, answer an iOS system alert, and assert on what you see. It talks to an [Appium](https://appium.io) 3 server that runs on the same
machine as venom, or on a Mac that venom reaches through an SSH tunnel. It uses the UiAutomator2 driver for an Android
emulator and the XCUITest driver for an iOS simulator.

Status: milestones M1 (#2396), M2 (#2397), M3 (#2398) and M4 (#2399). Android on an emulator and iOS on a simulator, on the same machine or on a Mac reached through SSH; web views, coordinates, deep links and alerts. The iOS calls are tested against a scripted server and were run on a simulator on 2026-10-28 and 2026-10-29 with the built-in Settings app and with Appium's UIKitCatalog (a `.zip` install, alerts, `scroll` with `until`, a web view). Not run on a simulator: a `.app` folder, coordinates, `openURL`, `hideKeyboard` and the alert `button` label (see "iOS"; the detailed run notes are not published).

Check that it works: run the hermetic tests (`go test ./executors/mobile/ -count=1`, no device needed), then the real
test against your own Appium and emulator (see "Run the tests"). The hermetic tests talk to a scripted fake server, so
they cannot prove that Appium 3 accepts every request: only the real test does.

## Host prerequisites

To set up a Mac for Android and iOS, see "Set up the Mac" below.

- Appium 3 with the UiAutomator2 driver (tested with Appium 3.9.0 and uiautomator2 8.7.2; Appium 2.x is not verified), started so that it **listens on the loopback address only** (127.0.0.1).
- An Android emulator (or a device) visible to Appium.
- The Android SDK build-tools (`sdkmanager "build-tools;34.0.0"`) when `mobile.app` names an `.apk`: UiAutomator2 needs `aapt2` to read it. Appium caches its tool lookup, so restart Appium after installing them. `ANDROID_HOME` must be set in the shell that starts Appium.
- venom on the same machine. M1 installs the app from a local file path that Appium reads, so both must share a host.

The executor needs no `--relaxed-security` flag on the Appium server.

## Variables

Set them under `vars: mobile:` in the suite (or in a `.venomrc`, or with `--var`). The session opens once per test case,
before its first `mobile` step, and closes after its last step, also when a step failed.

```yaml
vars:
  mobile:
    url: http://127.0.0.1:4723
    platform: android
    device: emulator-5554
    app: ./build/app-debug.apk
```

| Variable | Required | Meaning |
|---|---|---|
| `mobile.url` | yes | Address of the Appium server, `http` or `https`, no credentials in it. |
| `mobile.platform` | yes | `android` or `ios`. |
| `mobile.device` | yes | The device or emulator. On Android it is sent to Appium as both `appium:deviceName` and `appium:udid`. On iOS it is a simulator name (sent as `appium:deviceName` only) or a UDID (also sent as `appium:udid`). The executor never picks a device for you. Use the adb serial, such as `emulator-5554`; an AVD name such as `venom_test` is refused by Appium ("not in the list of connected devices"). |
| `mobile.osVersion` | no | Sent as `appium:platformVersion`. The report shows the version the server answers with, else this value. |
| `mobile.app` | no | Path of the app to install when the session opens: an `.apk` on Android, a simulator `.app` folder or its `.zip` on iOS. A relative path is resolved against the suite folder. |
| `mobile.appSHA256` | no | Expected SHA-256 of the `.apk` or `.zip`, in hex (upper or lower case). Needs `mobile.app`. Refused with a `.app` folder. |
| `mobile.appId` | no | Package id (Android) or bundle id (iOS) of the app. Without `mobile.app`, the session launches this installed app, and on iOS it is then required. With both, it is also the default for `launch` and `terminate`. |
| `mobile.ssh.host` | no | Host (name or address) of the Mac. When set, venom reaches the Appium server through an SSH tunnel to the host's own loopback, at the port of `mobile.url`, and copies the app there. Without it `mobile.url` is used as given. |
| `mobile.ssh.user` | with `ssh.host` | SSH user. |
| `mobile.ssh.key` | with `ssh.host` | Path of the private key. Key authentication only, no password. |
| `mobile.ssh.passphrase` | no | Passphrase of the key. Declare it under `secrets`. |
| `mobile.ssh.port` | no | SSH port, default 22. |
| `mobile.ssh.knownhosts` / `mobile.ssh.hostkey` | no | Host key check, as in the ssh executor: a known_hosts file, or one fixed key. Not both. With neither, venom warns once per run and does not check the host key. |
| `mobile.capabilities` | no | Extra Appium capabilities, as a map. See below. |
| `mobile.syncTimeout` | no | Seconds the executor waits for an element, default `10`. |
| `mobile.sessionTimeout` | no | Seconds allowed to open the session, default `180` on Android and `300` on iOS, where the first session builds WebDriverAgent. |
| `mobile.idleTimeout` | no | Seconds the server keeps the session open without a command, default `120` (sent as `appium:newCommandTimeout`). |

A negative number for any of the three timeouts is refused. A wrong value is refused before any request is sent.

### The app file and its SHA-256

With `mobile.app`, venom reads the file once and computes its SHA-256. If `mobile.appSHA256` is set and differs, the
test case fails **before any session is opened**, and the message gives both hashes. The computed hash is always shown in
the report (`appsha256`), so a report says which build ran. `mobile.appSHA256` without `mobile.app` is refused.

Without an SSH tunnel the hash covers the bytes venom read, and Appium installs from the path afterwards: a file replaced
in between would not be detected. With `mobile.ssh.*` the file is copied to the Mac and the hash covers the bytes sent
(see "Reaching the Mac through SSH").

### Extra capabilities

`mobile.capabilities` adds capabilities to the session request. A capability that the executor sets itself is refused
before any request, with a message naming the variable that owns it. The names are compared without the `appium:`
prefix, and inside `appium:options` too.

| Capability | Set by |
|---|---|
| `platformName` | `mobile.platform` |
| `deviceName`, `udid` | `mobile.device` |
| `platformVersion` | `mobile.osVersion` |
| `app` | `mobile.app` |
| `appPackage`, `bundleId` | `mobile.appId` |
| `automationName` | the executor (UiAutomator2 on Android, XCUITest on iOS) |
| `newCommandTimeout` | `mobile.idleTimeout` |
| `autoAcceptAlerts`, `autoDismissAlerts` | the alert action: the step handles alerts, so the session must not |

## Input

```yaml
  - action (mandatory): one of the 17 actions below
  - accessibilityId, text, id, xpath, uiautomator, css, name, predicate, classChain (one of them, see "Locators"): which element the step targets
  - choice (alert): accept (default) or dismiss, in any case
  - button (alert): the label of the button to press, instead of the default one
  - syncTimeout (optional): seconds to wait for the element in this step, overrides mobile.syncTimeout
  - value (fill): the text to type
  - clear (fill): true to clear the field first
  - attribute (read): read this attribute instead of the text
  - state (waitFor): present (default) or absent
  - direction (swipe, scroll): up, down, left or right
  - until (scroll): true to scroll until the element is visible
  - percent (swipe, scroll): the part of the area to cover, above 0 and at most 1, default 0.75
  - x, y (tap, swipe): the point to tap, or where the swipe starts; 0 is valid. Not with a locator
  - toX, toY (swipe): where a swipe from a point ends; give both, and no direction or percent
  - unit (tap, swipe): the unit of x, y, toX, toY: pixels (default, also px) or percent. Percent is of the window rect (0 to 100)
  - appId (launch, terminate): the package id, default mobile.appId
  - path (screenshot): the file to write, relative to the directory venom runs in
  - context (context): native, a web view name starting with WEBVIEW, or empty for the first web view
  - url (openURL): the deep link, scheme://..., at most 2048 bytes, no spaces or control characters
```

`text` is a **locator** (it finds an element by its visible text). The text you type is `value`.

### Locators

A step that targets an element gives **exactly one** locator. Zero or two is an error before any request.

| Field | Finds the element by |
|---|---|
| `accessibilityId` | its accessibility id (content description) |
| `text` | its exact visible text (a UiSelector `text`); quotes and backslashes in it are escaped |
| `id` | its resource id |
| `xpath` | an XPath over the view hierarchy |
| `uiautomator` | a UiAutomator selector, sent as written |
| `css` | a CSS selector, only inside a web view |
| `name` | iOS only: the element name |
| `predicate` | iOS only: an NSPredicate string, sent as written |
| `classChain` | iOS only: an iOS class chain, sent as written |

`id` and `uiautomator` are Android only; `name`, `predicate` and `classChain` are iOS only. A kind of the other platform
is refused before any request, with a message that says what to use instead. The iOS mapping is in "iOS".

An element is waited for: the executor asks again every 500 ms until it appears or the wait is over (10 s by default,
or the step's `syncTimeout`, or `mobile.syncTimeout`). If the element never appears, the failure names the locator and
the wait. An element found at the first try adds no delay.

## Actions and results

| Action | Needs | What it does | Result keys it fills |
|---|---|---|---|
| `tap` | a locator, or `x` and `y` (with optional `unit`) | clicks the element, or taps the point | `found` (with a locator) |
| `fill` | a locator, `value`, optional `clear` | types `value` into the element, after clearing it when `clear: true`. An empty `value` types nothing | `found` |
| `clear` | a locator | clears the element | `found` |
| `read` | a locator, optional `attribute` | reads the text of the element, or the attribute | `found`, `text` (no attribute) or `value` (attribute; a missing attribute reads as empty) |
| `count` | a locator | counts the matches now, without waiting. Zero is an answer, not a failure | `count`, `found` (true when above zero) |
| `waitFor` | a locator, optional `state` | waits for the element to be present (default) or absent | `found` |
| `swipe` | `direction`, optional locator and `percent`; or `x`, `y`, `toX`, `toY` (with optional `unit`) to drag from one point to another | swipes inside the element, or inside the visible area of the device when there is no locator; that area is the window inset by 10 percent on each side, so that the system gesture edges are never touched | `found` when a locator was given |
| `scroll` | `direction`, optional locator and `percent` | scrolls inside the element or the visible area (the window inset by 10 percent on each side, so that the system gesture edges are never touched). With `until: true` and a locator, it scrolls the visible area until that element is on screen, at most 10 scrolls | `found` when a locator was given |
| `back` | nothing | presses the Back button | none |
| `hideKeyboard` | nothing | hides the soft keyboard. If none is shown, that is not a failure | none |
| `launch` | `appId` or `mobile.appId` | brings the app to the front (`mobile: activateApp`) | none |
| `terminate` | `appId` or `mobile.appId` | stops the app (`mobile: terminateApp`) | none |
| `screenshot` | optional `path` | takes a screenshot. With `path`, writes the PNG there (mode 0600) | `found`, `screenshot` (the path) |
| `source` | nothing | returns the view hierarchy as XML | `found`, `source` |
| `context` | `context`: `native`, a web view name (`WEBVIEW_...`), or empty for the first web view | switches between the native app and a web view. With a web view, it waits (the step's wait) for it to be listed. A `css` locator needs a web view | `context` (the active one), `contexts` (the list the server gave; empty after a switch to native) |
| `openURL` | `url` | opens a deep link: `url` scheme://..., restricted to the app package when known (`mobile: deepLink`) | none |
| `alert` | optional `choice` and `button` | iOS only: accepts or dismisses the open system alert (see "iOS") | `text` (the alert text) |

An unknown action is refused with the list of the supported ones. Where an action differs on iOS, see "Differences from Android".

## Output

```yaml
  - result.text: the text read (read)
  - result.value: the attribute read (read with attribute)
  - result.count: the number of matches (count)
  - result.found: true when the step found its element, or the screen content
  - result.screenshot: the path written (screenshot with path)
  - result.source: the view hierarchy as XML (source)
  - result.timeseconds: duration of the step
  - result.err: the failure of the step, empty when it worked
  - result.platform, result.device, result.osversion: where the test ran
  - result.app, result.appsha256, result.appid: what ran
```

The run facts (`platform`, `device`, `osversion`, `app`, `appsha256`, `appid`) are repeated in **every** result, so each
test case of a report shows which build ran on which device. A key with no value is absent from the result.

Default assertion: `result.err ShouldBeEmpty`.

### Errors

- A **misuse** (two locators, an unknown action, a bad `direction`, a missing setting) is a venom error before any
  request is sent. The step fails with a message that names the cause.
- A **failure on the server or the device** (element not found, a refused command, an unreachable server, a call that
  hangs) lands in `result.err` as `action <name>: <message>`. The step fails on the default assertion and the run goes on.
  The Appium stack trace is never put in the result.

## Web views and hybrid apps

A hybrid app shows web pages inside a native screen. The `context` action switches between the two.

- `context: native` goes back to the native app.
- `context: WEBVIEW_<package>` switches to that web view.
- An empty `context` switches to the first web view the server lists.

For a web view, the executor waits up to the step's wait (`syncTimeout`, `mobile.syncTimeout`) for it to be listed, so a
page that loads late is found. If none shows up in time, the failure lists the contexts the server did report. The
result holds `context` (the active one) and `contexts` (the list the server gave).

Inside a web view the search is handled by chromedriver, so `css` and `xpath` are the locators to use. `text`,
`accessibilityId` and `uiautomator` are native strategies and are refused by the server in a web view
(checked on a device, 2026-10-28: `accessibilityId` came back as an error); what `id` means there (the HTML id) is not verified. After
`context: native`, a native locator works as before. `css` in the native context is refused with a message before any search. `source` returns the page HTML in a web view and the XML
hierarchy in the native context.

```yaml
      - type: mobile
        action: context
        context: WEBVIEW_com.example.app
      - type: mobile
        action: fill
        css: "#email"
        value: demo-user
      - type: mobile
        action: tap
        css: "button[type=submit]"
      - type: mobile
        action: context
        context: native
      - type: mobile
        action: waitFor
        accessibilityId: welcome_banner
```

### What the web view needs

- **Debugging must be on.** The app has to enable WebView debugging. Without it the server lists only `NATIVE_APP`, and
  a switch to a web view fails with that list.
- **A matching chromedriver.** UiAutomator2 drives a web view through a chromedriver whose version matches the WebView on
  the device. A mismatch makes the switch fail on the server. Set `appium:chromedriverExecutable` (a path on the Appium
  host) through `mobile.capabilities`, or in the server's own configuration:

  ```yaml
  vars:
    mobile:
      capabilities:
        appium:chromedriverExecutable: /opt/chromedrivers/chromedriver-114
  ```
- **Autodownload is the operator's choice.** The Appium server can fetch a chromedriver itself
  (`chromedriver_autodownload`). That needs a server option that the operator sets on the Appium server. venom never
  sends it and never needs `--relaxed-security` or `--allow-insecure`.

## Unlabelled apps and coordinates

An app that publishes no accessibility labels (some Flutter release builds, games, custom-drawn views) can only be
reached by coordinates. If the app has any labels, ids or text, prefer a locator: a coordinate breaks when the layout,
the screen size or the font changes.

`tap` and `swipe` take a point instead of a locator:

| Field | Meaning |
|---|---|
| `x`, `y` | the point to tap, or where the swipe starts. 0 is a valid value |
| `toX`, `toY` | where the swipe ends. Give both, and no `direction` or `percent` |
| `unit` | `pixels` (default, also `px`) or `percent` |

With `unit: percent` the point is a share, 0 to 100, of the window rect: `x: 50` and `y: 50` is the centre. On a
1080x2400 window that sends the point 540, 1200. Pixels are sent as given and the server bounds them. A point with a
locator, `x` without `y`, a negative value, NaN or an infinity, a pixel value above 10 million, or a percentage above 100
is refused before any request.

Percent coordinates and `swipe` or `scroll` without a locator read the window size from the server. Inside a web view that
size can be in the browser's units, not the device's pixels: switch back to native (`context: native`) before using
percent coordinates or a locator-less `swipe` and `scroll`. Not verified on a device: the hand run used pixel and percent
coordinates in the native context only.

```yaml
      - type: mobile
        action: tap
        x: 50
        y: 50
        unit: percent
      - type: mobile
        action: swipe
        x: 540
        y: 1800
        toX: 540
        toY: 600
```

## Deep links

`openURL` opens a link in the app, through Appium's `mobile: deepLink`:

```yaml
      - type: mobile
        action: openURL
        url: myapp://orders/42
```

- `url` must look like `scheme://...`, with no spaces or control characters, at most 2048 bytes. Anything else is
  refused before any request, and the message does not repeat the link (it can carry a token).
- The link is restricted to the app package (the step's `appId`, else `mobile.appId`) when one is known.
- Custom schemes are accepted: they are the point of a deep link. The link is passed to Appium's `mobile: deepLink`, which
  runs it through `am start`; venom builds no shell command. In the tested stack (Appium 3.9.0, uiautomator2 8.7.2,
  Android 14 emulator) five links carrying shell metacharacters reached the server, failed because nothing handled them,
  and created no file on the device. An older Appium or driver may not quote them: keep deep links from untrusted input
  out of suites.

## Limits

| Limit | Value |
|---|---|
| One call to the server | 60 s |
| Opening the session | 180 s (`mobile.sessionTimeout`) |
| Idle session on the server | 120 s (`mobile.idleTimeout`) |
| Pause between two attempts to find an element | 500 ms |
| Default wait for an element | 10 s (`mobile.syncTimeout`, `syncTimeout`) |
| Scrolls of `scroll` with `until` | 10 |
| A decoded screenshot | 12 MiB |
| Closing the session | 15 s |
| Each of the two diagnostic requests | 15 s |

A redirect from the server is not followed. The step's own `timeout:` still applies on top of these limits.

## Diagnostics on a failing step

When a step fails on the server or the device, the executor saves two files in the directory venom runs in:

- `screen.png`: a screenshot of the device;
- `screen.dump.xml`: the view hierarchy.

**Warning: both files show what is on the screen, including a secret you typed or that the app displays.** They are not
covered by the secret hiding of venom's reports. They are written with mode 0600 in the working folder. Do not publish
them as build artefacts. The next failing step replaces them; a passing step writes and deletes nothing. A
failure to fetch one file is a warning in the log and does not hide the step's own failure. A misuse error, or a session
the server no longer has, writes nothing.

Two venom runs started in the **same directory** overwrite each other's files. Run them from different directories.

## Reaching the Mac through SSH

Keep Appium listening on the Mac's loopback (`127.0.0.1:4723`); it has no password of its own. Set `mobile.ssh.host`, `mobile.ssh.user` and `mobile.ssh.key` and keep `mobile.url` as the address the Mac sees (`http://127.0.0.1:4723`): venom opens the tunnel when the test case starts and closes it when it ends. The tunnel listens on `127.0.0.1` only and always dials the Mac's loopback.

- **Host key:** set `mobile.ssh.knownhosts` or `mobile.ssh.hostkey`. A key that does not match refuses the connection before any request.
- **The app file:** with a tunnel, `mobile.app` is copied to `/tmp/venom-mobile-<random>/app.apk` (`app.zip` on iOS) on the Mac, installed from there and removed when the session ends. No suite value appears in a command run on the Mac. `mobile.appSHA256` is checked over the bytes sent; a mismatch fails before any session and removes the copy. A killed run can leave the folder behind: look for `/tmp/venom-mobile-*`.
- **Limits:** an `https://` URL through the tunnel is verified against `127.0.0.1`, so use `http://` inside the tunnel. The connection and the SSH handshake are bounded by `mobile.sessionTimeout` (180 s by default), so a host that does not answer fails the Setup instead of hanging it.
- **Plain http without a tunnel:** an `http://` URL to another machine prints one warning per run; a loopback URL prints none.

## Security notes

- **Appium must listen on loopback only.** An Appium server on the network lets anyone who reaches it control the
  emulator and the machine. In M1, venom connects to Appium on the same host.
- The app is installed from a **local file**, so Appium and venom must share a host in M1.
- **Screenshots, the dump and `result.source` show what is on the screen, including a secret you typed.** They are NOT
  covered by the secret hiding of venom's reports, the same limit as the web executor's trace. Keep `screen.png` and
  `screen.dump.xml` out of published artefacts and shared build folders.
- `fill` never echoes the typed text in a result or in the `-v` call log. With `-v`, each call to the server is logged (method, path, duration; never a body).
- Only Appium 3 endpoints are used, and the executor needs no `--relaxed-security`.
- **A web view's page content and the text typed into it can show in screenshots and in `result.source`**, like any
  native screen, and secret hiding does not cover them. Page text read with `read` is a result value like any other.
- **A deep-link URL is passed to Appium's `mobile: deepLink`, which runs it through `am start`.** venom itself never
  builds a shell command; the link is checked for shape and length, and restricted to the app package when one is known.
  Appium quoted shell metacharacters in the tested stack (Appium 3.9.0, uiautomator2 8.7.2, Android 14 arm64 emulator,
  measured 2026-10-28). An older Appium or driver may not, so keep deep links from untrusted input out of suites.
- **The chromedriver runs on the Appium host**, and which binary it is stays the operator's choice. venom sends no
  option that turns on `--relaxed-security` or `--allow-insecure`, and the server needs neither.
- **The text of an iOS alert** goes to `result.text` and nowhere else. It is not written to a log line or an error, but it
  is a result value like any other, so a secret in it is hidden only if it is a declared secret.
- **A `.app` folder is copied to the Mac as `app.zip`**, in a venom-named folder (`/tmp/venom-mobile-<random>`, mode 0700),
  with no suite value in any command run there. venom refuses a `.app` that holds a symbolic link, and computes no hash of
  a folder: its integrity is not checked. Give a `.zip` and `mobile.appSHA256` when it must be.
- **The XCUITest driver and WebDriverAgent run on the Mac** and are the operator's install. venom does not fetch or build
  them; the first session builds WebDriverAgent there.

## iOS

With `mobile.platform: ios` the same suite drives an app on an iOS **simulator** through the XCUITest driver. A real
iPhone is not covered. The iOS calls are implemented from the XCUITest driver's documented behaviour and tested against a
scripted server. They were run on a simulator (iPhone 17, iOS 26.5, Appium 3.9.0, XCUITest driver 12.22.3), in two runs:

- 2026-10-28, the built-in Settings app launched by bundle id, suite `examples/handrun-settings.yml`, 3 of 3 test
  cases passed: session open, `waitFor`, `tap`, swipe by direction, `terminate`, `launch`, the locators `text`, `name`,
  `predicate` and `classChain`, `count`, and `alert` with no alert open.
- 2026-10-29, Appium's UIKitCatalog v4.0.5 given as `UIKitCatalog.zip` with `mobile.appSHA256`, through the SSH tunnel,
  suite `examples/handrun-uikitcatalog.yml`, final status PASS: the `.zip` install, `alert` accept (the alert text
  came back in `result.text`) and dismiss, `scroll` with `until`, and a web view (`context` reported a `WEBVIEW`
  context, `css: body` counted more than 0, `context: native` went back). The iOS web view needed no chromedriver.

Not run on a simulator: a `.app` folder, coordinates, `openURL`, `hideKeyboard`, the alert `button` label, percent
coordinates. The detailed run notes are not published.
Rows of a Settings-like list are not `XCUIElementTypeCell` on iOS 26: write a class chain as `**/*[...]`, not with a cell type.
The first session took minutes (WebDriverAgent build), which is why the default session timeout is 300 s.

### Prerequisites on the Mac

- Xcode and its command line tools, and at least one simulator.
- Appium 3 with the XCUITest driver: `appium driver install xcuitest`. Keep Appium on the loopback address, as for Android.
- The first session builds WebDriverAgent and takes minutes. That is why `mobile.sessionTimeout` defaults to 300 s on iOS.
- A simulator needs no code signing.
- On an Apple Silicon Mac the app must have an arm64 slice, and the Android emulator images are arm64 there too. An iOS
  `.app` with only i386 or x86_64 slices is refused by Appium ("does not support the arm64 Simulator architecture"); an
  APK with only `armeabi-v7a` and `x86` libraries fails with `INSTALL_FAILED_NO_MATCHING_ABIS` (it needs `arm64-v8a`, or
  no native libraries). Cloud Grey's TheApp v1.10.0 (2019) failed both ways on the maker's Mac.
- A ready sample for trying iOS: Appium's UIKitCatalog (`UIKitCatalog.zip`, a simulator build with arm64 and x86_64
  slices, bundle id `com.example.apple-samplecode.UICatalog`), from the release page of
  <https://github.com/appium/ios-uicatalog>. The suite `examples/handrun-uikitcatalog.yml` runs on it.

### Settings that change on iOS

| Setting | On iOS |
|---|---|
| `mobile.device` | A simulator name (`iPhone 16`), or its UDID. A UDID is also sent as `appium:udid`; a name only as `appium:deviceName`. |
| `mobile.osVersion` | Sent as `appium:platformVersion`, as on Android. |
| `mobile.app` | A simulator `.app` folder, or its `.zip`. An **`.ipa` is a device build and does not install on a simulator**: it is refused before any request. Build a simulator `.app` instead. |
| `mobile.appSHA256` | Only for a `.zip`. A `.app` folder has no single file to hash, so the setting is refused with one. |
| `mobile.appId` | The bundle id. Without `mobile.app`, the installed app is launched by its bundle id and `mobile.appId` is then required. |
| `mobile.sessionTimeout` | 300 s by default. |

Over SSH, a `.app` folder is zipped on the machine that runs venom and copied to the Mac as `app.zip`; a `.zip` is copied
as it is. See "Reaching the Mac through SSH".

### Locators on iOS

| Field | Finds the element by |
|---|---|
| `accessibilityId` | its accessibility id |
| `text` | its label: sent as the predicate `label == "<text>"`, with quotes, backslashes and line breaks in the text escaped |
| `name` | its name |
| `predicate` | an NSPredicate string, sent as written |
| `classChain` | an iOS class chain, sent as written |
| `xpath` | an XPath over the view hierarchy |
| `css` | a CSS selector, only inside a web view |

`id` and `uiautomator` are refused on iOS; `name`, `predicate` and `classChain` are refused on Android. As on Android, a
step gives exactly one locator.

### Differences from Android

| Action | On iOS |
|---|---|
| `tap`, `fill`, `clear`, `read`, `count`, `waitFor`, `screenshot`, `source`, `context` | The same commands as on Android. |
| `tap` and `swipe` by `x`, `y` | The point is in points, not pixels. `tap` uses `mobile: tap`, `swipe` uses `mobile: dragFromToForDuration` over half a second. `unit: percent` is of the window rect, which iOS reports in points. |
| `swipe`, `scroll` by `direction` | `mobile: swipe` and `mobile: scroll`, on the element or the whole app. `percent` is refused: XCUITest has no such argument. |
| `scroll` with `until` | Repeats `mobile: scroll` up to 10 times. The server does not say when the list ends, so the loop stops on the element or the bound. |
| `back` | Refused: iOS has no back key. Tap the app's own back control. |
| `hideKeyboard` | `mobile: hideKeyboard`. A keyboard that is not shown is not a failure. |
| `launch`, `terminate` | `mobile: activateApp` and `mobile: terminateApp`, naming the app by `bundleId` (`appId` or `mobile.appId`). |
| `openURL` | `mobile: deepLink` with the `url`, and the `bundleId` when known. |
| `alert` | iOS only. On Android it is refused. |

### Alerts

An iOS app can raise a system alert, for instance a permission request. The `alert` action reads its text and then accepts
or dismisses it:

```yaml
      - type: mobile
        action: tap
        accessibilityId: enable_location
      - type: mobile
        action: alert
        choice: accept
        assertions:
          - result.err ShouldBeEmpty
          - result.text ShouldContainSubstring Location
      - type: mobile
        action: alert
        choice: dismiss
        button: Don't Allow
```

- `choice` is `accept` (default) or `dismiss`. Any other value is refused before any request.
- Without `button`, the step answers with the plain accept or dismiss command. With `button`, it presses the button with
  that label (`mobile: alert`).
- `result.text` holds the alert text. **An alert can show a secret** (a code, a name): the text is never written to a log
  line or an error message, and secret hiding of venom's reports covers only declared secrets.
- With no alert open, the step fails with `no alert open` in `result.err`.
- The session must not answer alerts by itself: `autoAcceptAlerts` and `autoDismissAlerts` in `mobile.capabilities` are
  refused, because the `alert` step handles them.
- A deep link on a simulator can raise a system confirmation alert ("Open in ...?"). Follow an `openURL` with an `alert`
  step when your app is opened that way, or the next step will find the alert in its way.

## Not yet

Planned, refused or absent today:

- A real iOS device: only simulators are in scope.
- Flutter on the iOS simulator is a debug build only: Flutter builds no release app for the simulator.
- Apps without an arm64 slice, on an Apple Silicon Mac.
- Password login over SSH: key authentication only.
- A chromedriver downloaded for you: for an Android web view the operator supplies a matching one.
- Not yet run on a simulator (see "iOS"; the detailed run notes are not published): the install of a `.app` folder (only a
  `.zip` ran), `x`/`y` and percent coordinates, `openURL`, `hideKeyboard`, and the alert `button` label. The suite
  `examples/handrun-ios.yml` is written for the `.app` folder install.

## Example

Every step below is a shape that the tests of this executor run.

```yaml
name: Mobile smoke
vars:
  mobile:
    url: http://127.0.0.1:4723
    platform: android
    device: emulator-5554
    osVersion: "14"
    app: ./build/app-debug.apk
    appSHA256: <sha-256 of the build, in hex>
    appId: com.example.app
    syncTimeout: 5

testcases:
  - name: the home screen is usable
    steps:
      - type: mobile
        action: waitFor
        accessibilityId: login_button
        syncTimeout: 20
      - type: mobile
        action: count
        id: com.example.app:id/item
        assertions:
          - result.count ShouldBeGreaterThan 0
      - type: mobile
        action: fill
        id: com.example.app:id/username
        value: demo-user
        clear: true
      - type: mobile
        action: hideKeyboard
      - type: mobile
        action: tap
        accessibilityId: login_button
      - type: mobile
        action: read
        id: com.example.app:id/title
        assertions:
          - result.err ShouldBeEmpty
          - result.text ShouldEqual Welcome
      - type: mobile
        action: scroll
        direction: down
        until: true
        text: Settings
      - type: mobile
        action: screenshot
        path: home.png
      - type: mobile
        action: back
      - type: mobile
        action: terminate
      - type: mobile
        action: launch
```

## Set up the Mac

Use this when venom runs on another machine (Windows, Linux) and the emulator and simulator run on a Mac. Everything
below is installed on the Mac. venom installs none of it.

1. **Appium 3** with both drivers: `appium driver install uiautomator2` and `appium driver install xcuitest`. Start it so
   that it listens on the loopback address only: `appium --address 127.0.0.1 --port 4723`. Set `ANDROID_HOME` in the
   shell that starts it.
2. **Android:** the Android SDK (platform-tools, `build-tools;34.0.0`) and an **arm64** emulator image on an Apple Silicon
   Mac. Start the emulator before the run.
3. **iOS:** Xcode, its command line tools and at least one simulator. Boot it, or let Appium boot it. The first session
   builds WebDriverAgent and takes minutes.
4. **Android web views:** a chromedriver that matches the WebView on the emulator, and its path in
   `appium:chromedriverExecutable` (see "What the web view needs"). iOS web views need none.
5. **SSH:** a key the Mac accepts for the user you give in `mobile.ssh.user`. Password login is not supported.

Gotchas seen on a real Mac:

- **The first run of a tool can stall over SSH.** macOS Gatekeeper may ask for a confirmation that nobody sees on an SSH
  session, and the binary waits. Run `flutter --version` (or the tool in question) once in a Terminal on the Mac itself
  and answer the prompt. Tools installed with a Homebrew cask can also carry the quarantine flag. Removing it with
  `xattr -dr com.apple.quarantine <folder>` is your decision: do it only for a folder you trust.
- **Apps need an arm64 slice** on an Apple Silicon Mac (see "Prerequisites on the Mac").
- **Flutter builds only debug apps for the iOS simulator.** A Flutter release build is possible on Android only.
- **CocoaPods needs a UTF-8 locale** (`export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8`), for instance when it runs from a
  script started over SSH.

## Sample apps

Public apps that the maker's runs used. Each is arm64-ready.

| App | Platform | Where from |
|---|---|---|
| ApiDemos | Android | Appium's `ApiDemos-debug.apk`, from the releases of <https://github.com/appium/android-apidemos> |
| UIKitCatalog v4.0.5 | iOS simulator | `UIKitCatalog.zip`, from the releases of <https://github.com/appium/ios-uicatalog> |
| Flutter counter app | both | `flutter create`, built by `examples/build-sample-apps.sh` (Android release APK, iOS simulator debug app) |
| React Native app | both | `react-native init`, built by the same script (Android release APK, iOS Release simulator app) |

The script runs on the Mac. It installs the Flutter SDK and CocoaPods if they are missing, builds the four apps from the
frameworks' own templates and lists them at the end. Run `flutter --version` once by hand first (see "Set up the Mac").

The suites are in `examples/`: `examples/flutter.yml` and `examples/rn.yml` run unchanged on both platforms, with the variable file of
each platform (`examples/android.vars.example.yml`, `examples/ios.vars.example.yml`: fill the `<...>` values); `examples/failing.yml` fails on
purpose to show the diagnostics files. The detailed run notes are not published.

## Run the tests

Hermetic tests (no device, no network; part of `go test ./...`):

```bash
go test ./executors/mobile/ -count=1
```

Real test, behind the build tag `mobile`. It needs Appium 3 with UiAutomator2 on loopback and an emulator, and it reads
its settings from the environment. It skips with a message when one is unset.

```bash
export VENOM_MOBILE_URL=http://127.0.0.1:4723
export VENOM_MOBILE_DEVICE=emulator-5554
export VENOM_MOBILE_APP=/path/to/sample.apk        # or VENOM_MOBILE_APPID=<package id of an installed app>
export VENOM_MOBILE_LOCATOR_KIND=accessibilityId   # accessibilityId, text, id, xpath or uiautomator
export VENOM_MOBILE_LOCATOR_VALUE=<an element on the first screen>
export VENOM_MOBILE_EXPECT_TEXT=<its text>         # optional
export VENOM_MOBILE_OSVERSION=14                   # optional
go test -tags mobile ./executors/mobile/ -run Real -v -count=1
```

It opens a session, finds and reads the element, checks that a passing step leaves no diagnostics, fails one step on
purpose and checks that `screen.png` and `screen.dump.xml` appear, then closes the session.

Two more real tests share the session variables above (`VENOM_MOBILE_URL`, `_DEVICE`, `_APP` or `_APPID`):

| Test | Extra variables | What it checks |
|---|---|---|
| `TestReal_CoordinatesAndDeepLink` | none | percent and pixel swipes and a tap at 50 % / 50 % succeed; `openURL venomtest://nothing/handles/this` comes back as a non-empty `result.err` |
| `TestReal_IOSSession` | `VENOM_MOBILE_PLATFORM=ios`, `VENOM_MOBILE_DEVICE` a simulator name, `VENOM_MOBILE_APPID` a bundle id (or `VENOM_MOBILE_APP`), `VENOM_MOBILE_IOS_LOCATOR` (accessibility id on the first screen, default `General` for the Settings app) | `waitFor`, swipe up, `terminate`, `launch`, and `alert` with none open answers `no alert open`; skipped unless `VENOM_MOBILE_PLATFORM` is `ios` |
| `TestReal_WebViewContext` | `VENOM_MOBILE_CHROMEDRIVER` (path of a matching chromedriver on the Appium host) and `VENOM_MOBILE_WEBVIEW_LOCATOR` (accessibility id that opens a web view screen) | `context` reaches a `WEBVIEW...`, `css: body` counts more than 0, `context: native` returns `NATIVE_APP`; skipped unless both are set |
