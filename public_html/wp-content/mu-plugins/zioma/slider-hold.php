<?php
/**
 * Hold carousel auto-sliding until the visitor first touches, scrolls or moves
 * the mouse (or 'slider_hold_timeout' seconds pass).
 *
 * The homepage carousels (Swiper) start auto-advancing while the page is still
 * loading. Each slide change re-lays out and repaints the first screen, which
 * pushes Speed Index up (8 s on mobile PageSpeed, where the top slider changes
 * slide mid-test) and adds main-thread work. Swiper keeps its instance on the
 * container element (el.swiper), so this stops the autoplay of every running
 * carousel right after the theme starts it and restarts it on the first
 * interaction. Slides, sizes and styles are untouched; only the first
 * auto-advance waits for the visitor. A carousel that is not Swiper, or that
 * the theme never set to autoplay, is left alone.
 *
 * Obeys 'slider_hold' (off / trial / on) and runs only in the contexts listed
 * in 'slider_hold_contexts'.
 */

defined( 'ABSPATH' ) || exit;

add_action(
	'wp_footer',
	function () {
		if ( ! zioma_perf_active() || ! zioma_mode_active( 'slider_hold' ) ) {
			return;
		}

		$config   = zioma_perf_config();
		$contexts = isset( $config['slider_hold_contexts'] ) ? (array) $config['slider_hold_contexts'] : array();
		if ( ! array_intersect( $contexts, zioma_perf_contexts() ) ) {
			return;
		}
		$timeout = isset( $config['slider_hold_timeout'] ) ? max( 1, (int) $config['slider_hold_timeout'] ) : 10;
		?>
		<script data-no-optimize="1" data-no-defer="1" data-cfasync="false">(function(){
			var held = [], released = false, polls = 0;
			var events = ['touchstart', 'scroll', 'wheel', 'mousedown', 'mousemove', 'keydown'];
			function release() {
				if (released) { return; }
				released = true;
				events.forEach(function (e) { removeEventListener(e, release, { passive: true }); });
				held.forEach(function (s) { try { if (!s.destroyed) { s.autoplay.start(); } } catch (e) {} });
			}
			function hold() {
				if (released) { return; }
				document.querySelectorAll('.swiper, .swiper-container').forEach(function (el) {
					var s = el.swiper;
					if (s && s.autoplay && s.autoplay.running && held.indexOf(s) === -1) {
						try { s.autoplay.stop(); held.push(s); } catch (e) {}
					}
				});
			}
			events.forEach(function (e) { addEventListener(e, release, { passive: true }); });
			var timer = setInterval(function () {
				hold();
				if (released || ++polls > <?php echo (int) $timeout * 10; ?>) { clearInterval(timer); }
			}, 100);
			setTimeout(release, <?php echo (int) $timeout * 1000; ?>);
		})();</script>
		<?php
	},
	PHP_INT_MAX
);
