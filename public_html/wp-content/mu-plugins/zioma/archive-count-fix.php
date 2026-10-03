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
