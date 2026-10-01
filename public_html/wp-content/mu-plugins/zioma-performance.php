<?php
/**
 * Plugin Name: Zioma Performance
 * Description: Speed and bug fixes for zioma.ir: per-page asset trimming, eager loading of above-the-fold images, LiteSpeed cache guard for Ajax requests, admin diagnostics (?zioma_assets=1).
 * Version: 1.0.0
 *
 * A must-use plugin so it runs whichever theme is active and survives theme
 * and plugin updates. The code lives in zioma/ next to this file.
 */

defined( 'ABSPATH' ) || exit;

require_once __DIR__ . '/zioma/cache-compat.php';
require_once __DIR__ . '/zioma/performance.php';
require_once __DIR__ . '/zioma/lazyload.php';
require_once __DIR__ . '/zioma/asset-inspector.php';
