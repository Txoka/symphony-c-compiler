#include <symphony.h>

/*
 * Fast static Mandelbrot renderer for Dynphony/Symphony.
 *
 * Optimizations:
 *   - 1024x768 resolution (SCREEN_SETTING = 255)
 *   - Q10 fixed point: all relevant products fit signed 32-bit
 *   - no division in the pixel/iteration hot loops
 *   - x coordinates precomputed once; y advanced incrementally
 *   - symmetry about the real axis: only half the rows are iterated
 *   - exact main-cardioid and period-2-bulb interior rejection
 *   - cardioid test rearranged to stay within signed 32-bit
 *   - iteration body is fully inlined
 *   - framebuffer pointer increments instead of pixel index multiplies
 *   - both symmetric pixels written together
 *   - static palette
 *
 * Pixel32 layout follows the existing Symphony examples:
 *     R << 24 | G << 16 | B << 8
 */

#define SCREEN_SETTING 255u

#define WIDTH       (4u * (SCREEN_SETTING + 1u))
#define HEIGHT      (3u * (SCREEN_SETTING + 1u))
#define PIXEL_COUNT (WIDTH * HEIGHT)

#define FIX_SHIFT 10
#define FIX_ONE   (1 << FIX_SHIFT)

#define MAX_ITER 96u

#define X_MIN (-2560)  /* -2.5 */
#define X_MAX (1024)   /*  1.0 */
#define Y_MAX (1229)   /*  1.2 */

#define ESCAPE2 (4 * FIX_ONE)

static unsigned int framebuffer[PIXEL_COUNT];
static int x_coord[WIDTH];
static unsigned int palette[MAX_ITER + 1u];


static unsigned int pack_rgb(
    unsigned int r,
    unsigned int g,
    unsigned int b
)
{
    return
          (r << 24)
        | (g << 16)
        | (b << 8);
}


static void initialize_palette(void)
{
    unsigned int i;
    unsigned int phase;
    unsigned int r;
    unsigned int g;
    unsigned int b;

    i = 0u;

    while (i < MAX_ITER) {
        phase = (i * 1536u) / MAX_ITER;

        if (phase < 256u) {
            r = 0u;
            g = phase;
            b = 255u;
        }
        else if (phase < 512u) {
            r = 0u;
            g = 255u;
            b = 511u - phase;
        }
        else if (phase < 768u) {
            r = phase - 512u;
            g = 255u;
            b = 0u;
        }
        else if (phase < 1024u) {
            r = 255u;
            g = 1023u - phase;
            b = 0u;
        }
        else if (phase < 1280u) {
            r = 255u;
            g = 0u;
            b = phase - 1024u;
        }
        else {
            r = 1535u - phase;
            g = 0u;
            b = 255u;
        }

        palette[i] = pack_rgb(r, g, b);
        i += 1u;
    }

    palette[MAX_ITER] = 0x00000000u;
}


/*
 * Precompute x exactly as the straightforward mapping would:
 *
 *   X_MIN + (X_MAX-X_MIN)*x/(WIDTH-1)
 *
 * This division occurs only WIDTH times at startup.
 */
static void initialize_x_coordinates(void)
{
    unsigned int x;

    x = 0u;

    while (x < WIDTH) {
        x_coord[x] =
            X_MIN
            +
            (
                (X_MAX - X_MIN) * (int)x
            )
            /
            (int)(WIDTH - 1u);

        x += 1u;
    }
}


/*
 * Exact Q10 tests for the two large analytically-known interior regions.
 *
 * Period-2 bulb:
 *
 *     (x + 1)^2 + y^2 <= 1/16
 *
 * Main cardioid:
 *
 *     q * (q + (x - 1/4)) <= y^2 / 4
 *     q = (x - 1/4)^2 + y^2
 *
 * q is Q10.  The cardioid comparison is rearranged as:
 *
 *     q * (q + x - 1/4) <= y^2 / 4
 *
 * Both sides are Q20 integer products.  This avoids an extra
 * fixed-point multiply/shift and remains comfortably in signed 32-bit.
 */
static int definitely_inside(
    int cx,
    int cy,
    int cy2
)
{
    int t;
    int q;

    t = cx + FIX_ONE;

    if (
        t * t + cy2
        <=
        (FIX_ONE * FIX_ONE) / 16
    ) {
        return 1;
    }

    t = cx - (FIX_ONE / 4);

    q =
          ((t * t) >> FIX_SHIFT)
        + (cy2 >> FIX_SHIFT);

    if (
        q * (q + t)
        <=
        (cy2 >> 2)
    ) {
        return 1;
    }

    return 0;
}


/*
 * Hot Mandelbrot iteration.
 *
 * No function calls, division, or coordinate arithmetic here.
 */
static unsigned int iterate_pixel(
    int cx,
    int cy,
    int cy2
)
{
    int zx;
    int zy;
    int zx2;
    int zy2;
    int zxy;

    unsigned int iteration;

    if (definitely_inside(cx, cy, cy2))
        return MAX_ITER;

    zx = 0;
    zy = 0;

    zx2 = 0;
    zy2 = 0;

    iteration = 0u;

    while (
        iteration < MAX_ITER
        &&
        zx2 + zy2 <= ESCAPE2
    ) {
        zxy =
            (zx * zy)
            >> FIX_SHIFT;

        zy =
            (zxy << 1)
            + cy;

        zx =
            zx2
            - zy2
            + cx;

        zx2 =
            (zx * zx)
            >> FIX_SHIFT;

        zy2 =
            (zy * zy)
            >> FIX_SHIFT;

        iteration += 1u;
    }

    return iteration;
}


static void render(void)
{
    unsigned int half;
    unsigned int y;
    unsigned int x;

    unsigned int top_index;
    unsigned int bottom_index;

    unsigned int *top;
    unsigned int *bottom;

    int cy;
    int cy2;

    unsigned int iteration;
    unsigned int color;


    /*
     * HEIGHT is even at the configured 1024x768 resolution.
     * Render rows 0..HEIGHT/2 and mirror them.
     */
    half = HEIGHT / 2u;

    y = 0u;

    while (y <= half) {
        /*
         * Positive half-plane only.  Mapping is exactly symmetric:
         *
         *   y=0      -> +Y_MAX
         *   y=half   -> 0
         */
        cy =
            Y_MAX
            -
            (
                Y_MAX * (int)y
            )
            /
            (int)half;

        cy2 = cy * cy;

        top_index =
            y * WIDTH;

        bottom_index =
            (HEIGHT - 1u - y)
            * WIDTH;

        top =
            framebuffer
            + top_index;

        bottom =
            framebuffer
            + bottom_index;


        x = 0u;

        while (x < WIDTH) {
            iteration =
                iterate_pixel(
                    x_coord[x],
                    cy,
                    cy2
                );

            color =
                palette[iteration];

            top[x] =
                color;

            /*
             * On the center row top == bottom.  Avoid the duplicate store.
             */
            if (top != bottom)
                bottom[x] = color;

            x += 1u;
        }

        /*
         * Explicitly refresh after each completed mirrored scanline pair.
         * This preserves the visible slow scan while rendering.
         */
        screen(
            1u,
            (unsigned int)framebuffer
        );

        y += 1u;
    }
}


int main(void)
{
    /*
     * Configure the display first.  The framebuffer is static-zeroed, so
     * the screen remains black while the image is calculated.
     */
    screen(
        1u,
        (unsigned int)framebuffer
    );

    screen(
        2u,
        SCREEN_SETTING
    );

    screen(
        0u,
        3u
    );


    initialize_palette();
    initialize_x_coordinates();

    render();

    return 0;
}
