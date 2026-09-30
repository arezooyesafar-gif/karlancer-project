<?php
/**
 * Plugin Name: Mizbanfa One-Click Login
 * Description: Secure, time-limited, single-use admin auto-login from the Mizbanfa client area. Installed and managed by the hosting panel.
 * Author: Mizbanfa
 * Version: 1.0.0
 *
 * The shared secret is injected by the provisioner (replaces REMOVED).
 * Login URL: /?mfa_autologin=1&u=<login>&exp=<unixts>&sig=<hmac_sha256(login|exp, secret)>
 */

if (!defined('ABSPATH')) {
    exit;
}

define('MFA_AUTOLOGIN_SECRET', 'REMOVED');

add_action('init', function () {
    if (empty($_GET['mfa_autologin'])) {
        return;
    }
    $login = isset($_GET['u']) ? (string) $_GET['u'] : '';
    $exp = isset($_GET['exp']) ? (int) $_GET['exp'] : 0;
    $sig = isset($_GET['sig']) ? (string) $_GET['sig'] : '';

    if ($login === '' || $exp <= 0 || $sig === '') {
        return;
    }
    if (time() > $exp) {
        wp_die('لینک ورود منقضی شده است.');
    }

    $secret = MFA_AUTOLOGIN_SECRET;
    if ($secret === '' || $secret === '__MFA_' . 'SECRET__') {
        return; // not provisioned
    }

    $expected = hash_hmac('sha256', $login . '|' . $exp, $secret);
    if (!hash_equals($expected, $sig)) {
        wp_die('لینک ورود نامعتبر است.');
    }

    // Single-use guard.
    $usedKey = 'mfa_used_' . md5($sig);
    if (get_transient($usedKey)) {
        wp_die('این لینک ورود قبلاً استفاده شده است.');
    }

    $user = get_user_by('login', $login);
    if (!$user) {
        wp_die('کاربر یافت نشد.');
    }

    set_transient($usedKey, 1, 600);

    wp_set_current_user($user->ID);
    wp_set_auth_cookie($user->ID, false);
    do_action('wp_login', $user->user_login, $user);

    wp_safe_redirect(admin_url());
    exit;
});
