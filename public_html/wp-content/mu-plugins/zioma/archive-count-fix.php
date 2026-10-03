<?php
/**
 * Fix the archive result count that reads "محصولی یافت نشد" next to real products.
 *
 * A host mu-plugin (wp-rand-prepend.php: mfm_optimize_query / mfm_search_pre)
 * hooks posts_pre_query and returns cached product rows without setting
 * found_posts, so Archive V4's own WP_Query comes back with the products listed
 * but found_posts = 0. The theme's ArchiveHeader then correctly prints
 * "no products found" and the loop head count is wrong (and load-more can break,
 * because max_num_pages is 0). The defect is in the host query cache, not the
 * theme or our code.
 *
 * On shop / product-taxonomy pages only, when a product query reports
 * found_posts <= 0 but actually returned posts AND the page is not full
 * (post_count < posts_per_page, so there is no further page), set found_posts to
 * the true total we can be certain of. This never lowers a correct count and
 * never guesses a total it cannot know — a full page is left untouched — so it
 * only ever turns the wrong "not found" into the correct number, with no visible
 * change anywhere the count was already right. Obeys the 'archive_count_fix'
 * switch (off / trial / on) and the ?zioma_perf=off escape hatch.
 */

defined( 'ABSPATH' ) || exit;

add_filter(
	'found_posts',
	function ( $found, $query ) {
		if ( ! zioma_mode_active( 'archive_count_fix' ) ) {
			return $found;
		}
		if ( (int) $found > 0 || ! ( $query instanceof WP_Query ) ) {
			return $found;
		}
		if ( is_admin() || ! function_exists( 'is_shop' ) ) {
			return $found;
		}
		if ( ! ( is_shop() || is_product_taxonomy() || is_post_type_archive( 'product' ) ) ) {
			return $found;
		}
		if ( 'product' !== $query->get( 'post_type' ) ) {
			return $found;
		}

		$per   = (int) $query->get( 'posts_per_page' );
		$count = (int) $query->post_count;
		$paged = max( 1, (int) $query->get( 'paged' ) );

		// Certain only when the page is not full: there is no further page, so the
		// total is everything before this page plus what this page returned.
		if ( $count > 0 && $per > 0 && $count < $per ) {
			return ( $paged - 1 ) * $per + $count;
		}

		return $found;
	},
	20,
	2
);

/**
 * Let the real product count (and therefore load-more / pagination for
 * categories with more than one page) work despite the server-level script.
 *
 * `wp-rand-prepend.php` runs as a CloudLinux auto_prepend file (outside the
 * account, so it cannot be edited here) and short-circuits product queries via
 * posts_pre_query (`mfm_search_pre`) plus a pre_get_posts tweak
 * (`mfm_optimize_query`), which leaves found_posts = 0 and makes the archive
 * think there is only one page. For product archive requests only, this removes
 * those two hooks so WooCommerce/Archive V4 queries run normally and count their
 * real totals. Removing by exact name/priority is a safe no-op if the script
 * ever changes. Staged behind 'archive_loadmore_fix' (off / trial / on) because
 * it changes how those queries run and must be verified on a real multi-page
 * category first (?zioma_trial=1).
 */
add_action(
	'pre_get_posts',
	function ( $query ) {
		if ( ! zioma_mode_active( 'archive_loadmore_fix' ) ) {
			return;
		}
		if ( is_admin() || ! ( $query instanceof WP_Query ) || ! function_exists( 'is_shop' ) ) {
			return;
		}

		$is_product_query = ( 'product' === $query->get( 'post_type' ) );
		$is_archive_main  = ( $query->is_main_query() && ( is_shop() || is_product_taxonomy() || is_post_type_archive( 'product' ) ) );

		if ( ! $is_product_query && ! $is_archive_main ) {
			return;
		}

		// Drop the server script's query short-circuit for this request so the
		// real count is computed. No-op if the hook is absent or renamed.
		remove_action( 'pre_get_posts', 'mfm_optimize_query', 10 );
		remove_filter( 'posts_pre_query', 'mfm_search_pre', 10 );
	},
	1
);
