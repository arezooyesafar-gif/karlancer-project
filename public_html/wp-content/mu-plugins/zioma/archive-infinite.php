<?php
/**
 * Load more products automatically while scrolling a product category.
 *
 * Archive V4 shows its own "load more" button under the product grid. The
 * client wants the next products to appear on scroll instead, without losing
 * the theme's filters and sorting, so this does not load anything itself: when
 * the theme's button comes within 600 px of the screen, it clicks that button,
 * and the theme fetches the next page exactly as a tap would (with whatever
 * filter or sort is active). Nothing is restyled.
 *
 * The button is found by what it is, not by a fixed class: a visible button or
 * link after the last product card, inside the product list, whose class
 * mentions load-more or whose text contains "بیشتر" (elsewhere in the main
 * column only by class, so a "show more" link in the category description is
 * never clicked), outside the sidebar, modals, cards, header and footer.
 * If nothing matches (numbered pagination, or no further page) it does nothing.
 * It stops after two clicks that bring no new cards.
 *
 * Obeys 'archive_infinite' (off / trial / on) on product archive pages.
 */

defined( 'ABSPATH' ) || exit;

add_action(
	'wp_footer',
	function () {
		if ( ! zioma_perf_active() || ! zioma_mode_active( 'archive_infinite' ) || ! in_array( 'product_archive', zioma_perf_contexts(), true ) ) {
			return;
		}
		?>
		<script data-no-optimize="1" data-no-defer="1" data-cfasync="false">(function(){
			var CARDS = '.prk-av4-card, ul.products > li.product';
			var busy = false, misses = 0, ticking = false, seen = 0;
			function count() { return document.querySelectorAll(CARDS).length; }
			// The theme's button: inside the product list (class or "بیشتر" text), or
			// elsewhere in the main column only by its load-more class, so a "show
			// more" link in the category description is never clicked.
			function find(root, last, classOnly) {
				if (!root) { return null; }
				var all = root.querySelectorAll('button, a, [role="button"]');
				for (var i = 0; i < all.length; i++) {
					var el = all[i];
					if (!(last.compareDocumentPosition(el) & 4) || el.closest('aside, .prk-modal, .prk-modal-overlay, header, footer, form, ' + CARDS)) { continue; }
					var cls = String(el.className || ''), text = (el.textContent || '').trim();
					if (!(/load-?more|loadmore/i.test(cls) || (!classOnly && /بیشتر/.test(text)))) { continue; }
					if (el.disabled || el.getAttribute('aria-disabled') === 'true' || !el.getClientRects().length) { continue; }
					return el;
				}
				return null;
			}
			function button() {
				var cards = document.querySelectorAll(CARDS);
				if (!cards.length) { return null; }
				var last = cards[cards.length - 1];
				var list = last.closest('.prk-av4-loop') || (last.parentElement && last.parentElement.parentElement);
				return find(list, last, false) || find(last.closest('.prk-av4-main, main'), last, true);
			}
			function check() {
				ticking = false;
				// A filter or sort reloaded the grid: start counting misses again.
				if (count() !== seen) { seen = count(); misses = 0; }
				if (busy || misses >= 2) { return; }
				var btn = button();
				if (!btn || btn.getBoundingClientRect().top > innerHeight + 600) { return; }
				busy = true;
				var before = count(), started = Date.now();
				btn.click();
				(function wait() {
					if (count() > before) { busy = false; misses = 0; setTimeout(check, 300); return; }
					if (Date.now() - started > 8000) { busy = false; misses++; return; }
					setTimeout(wait, 250);
				})();
			}
			addEventListener('scroll', function () {
				if (!ticking) { ticking = true; requestAnimationFrame(check); }
			}, { passive: true });
			addEventListener('load', function () { setTimeout(check, 500); });
		})();</script>
		<?php
	},
	PHP_INT_MAX
);
