<?php
/**
 * Keep LiteSpeed Cache from lazy-loading the images a visitor sees first.
 *
 * Lighthouse (1 Oct 2026) showed LiteSpeed's lazy-load holding back the main
 * product image, the LCP element, by 3.4 s on mobile and 5.7 s on desktop, and
 * the first product cards on category pages. Lazy-loading only changes when an
 * image starts downloading, so excluding these images changes nothing visually.
 *
 * LiteSpeed skips any <img> whose src contains one of the excluded strings, so
 * each image is excluded by its upload path without extension, which matches
 * the original and every generated size.
 */

defined( 'ABSPATH' ) || exit;

/**
 * Upload-relative path of an attachment without extension or the -scaled /
 * -rotated suffix, e.g. "/2026/04/oven-dt-730-1", or '' if unknown.
 */
function zioma_lazy_image_stem( $attachment_id ) {
	$file = (string) get_post_meta( $attachment_id, '_wp_attached_file', true );
	if ( '' === $file ) {
		return '';
	}

	$stem = preg_replace( '/(-scaled|-rotated)?\.[a-z0-9]+$/i', '', $file );

	return '/' . ltrim( $stem, '/' );
}

/**
 * Attachment IDs shown at the top of the current page.
 */
function zioma_lazy_above_fold_images() {
	if ( ! function_exists( 'is_product' ) ) {
		return array();
	}

	if ( is_product() ) {
		$product = wc_get_product( get_queried_object_id() );
		if ( ! $product ) {
			return array();
		}
		$gallery = $product->get_gallery_image_ids();

		return array( $product->get_image_id() ? $product->get_image_id() : reset( $gallery ) );
	}

	if ( is_shop() || is_product_taxonomy() ) {
		$tweaks = zioma_perf_config()['tweaks'];
		$count  = isset( $tweaks['eager_product_cards'] ) ? max( 0, (int) $tweaks['eager_product_cards'] ) : 4;
		$posts  = array_slice( (array) $GLOBALS['wp_query']->posts, 0, $count );

		return array_map( 'get_post_thumbnail_id', $posts );
	}

	return array();
}

add_filter(
	'litespeed_media_lazy_img_excludes',
	function ( $excludes ) {
		if ( ! zioma_perf_active() || empty( zioma_perf_config()['tweaks']['eager_above_fold'] ) ) {
			return $excludes;
		}
		$excludes = (array) $excludes;

		foreach ( array_filter( zioma_lazy_above_fold_images() ) as $attachment_id ) {
			$stem = zioma_lazy_image_stem( $attachment_id );
			if ( '' === $stem ) {
				continue;
			}
			$excludes[] = $stem;

			// Also match the URL if the theme printed it percent-encoded.
			$encoded = implode( '/', array_map( 'rawurlencode', explode( '/', $stem ) ) );
			if ( $encoded !== $stem ) {
				$excludes[] = $encoded;
			}
		}

		return $excludes;
	}
);
