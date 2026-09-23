#include <symphony.h>

/*
 * Pong for Dynphony/Symphony.
 *
 * Controls (Localized keyboard mode):
 *   W / S  - left paddle
 *   O / L  - right paddle
 *
 * keyboard() is treated as an event queue.  The low byte is the localized
 * ASCII key value; bit 8 is assumed to distinguish key-up from key-down.
 *
 * If your keyboard component encodes key-up differently, only
 * poll_keyboard() needs changing.
 */

#define SCREEN_SETTING 32u

#define WIDTH       (4u * (SCREEN_SETTING + 1u))
#define HEIGHT      (3u * (SCREEN_SETTING + 1u))
#define PIXEL_COUNT (WIDTH * HEIGHT)

#define BLACK 0x00000000u
#define WHITE 0xFFFFFFFFu

#define PADDLE_W 2
#define PADDLE_H 18
#define PADDLE_MARGIN 5
#define PADDLE_SPEED 55

#define BALL_SIZE 2

/*
 * Ball velocity in pixels/second.
 * Position is kept in Q16.16 so movement is independent of CPU/emulator speed.
 */
#define BALL_SPEED_X 48
#define BALL_SPEED_Y 30

#define POS_SHIFT 16
#define POS_ONE   (1 << POS_SHIFT)

/* Advance one logical 60 Hz game step for every rendered frame. */
#define FRAME_DT_NS 16666667u

#define KEY_W 0x77u
#define KEY_S 0x73u
#define KEY_O 0x6Fu
#define KEY_L 0x6Cu

static unsigned int framebuffer[PIXEL_COUNT];

static int left_y;
static int right_y;

/* Q16.16 paddle positions, just like the ball. */
static int left_fy;
static int right_fy;

static int ball_x;
static int ball_y;

/* Q16.16 ball position and pixels/second velocity. */
static int ball_fx;
static int ball_fy;
static int ball_dx;
static int ball_dy;

static unsigned int w_down;
static unsigned int s_down;
static unsigned int o_down;
static unsigned int l_down;


/* ------------------------------------------------------------------------- */
/* Drawing                                                                   */
/* ------------------------------------------------------------------------- */

static void clear_rect(
    int x,
    int y,
    int w,
    int h
)
{
    int yy;
    int xx;
    unsigned int *row;

    yy = 0;

    while (yy < h) {
        row =
            framebuffer
            + (unsigned int)(y + yy) * WIDTH
            + (unsigned int)x;

        xx = 0;

        while (xx < w) {
            row[xx] = BLACK;
            xx += 1;
        }

        yy += 1;
    }
}


static void restore_center_line(
    int x,
    int y,
    int w,
    int h
)
{
    int yy;
    int line_x;

    line_x = WIDTH / 2;

    /*
     * Nothing to restore if this rectangle does not cross the center line.
     */
    if (
        line_x < x
        ||
        line_x >= x + w
    ) {
        return;
    }

    yy = y;

    while (yy < y + h) {
        /*
         * draw_center_line() places one pixel every four rows starting at 1.
         */
        if ((yy & 3) == 1) {
            framebuffer[
                (unsigned int)yy * WIDTH
                + (unsigned int)line_x
            ] = WHITE;
        }

        yy += 1;
    }
}

static void fill_rect(
    int x,
    int y,
    int w,
    int h
)
{
    int yy;
    int xx;
    unsigned int *row;

    yy = 0;

    while (yy < h) {
        row =
            framebuffer
            + (unsigned int)(y + yy) * WIDTH
            + (unsigned int)x;

        xx = 0;

        while (xx < w) {
            row[xx] = WHITE;
            xx += 1;
        }

        yy += 1;
    }
}


static void draw_center_line(void)
{
    int y;
    unsigned int index;

    y = 1;

    while (y < HEIGHT) {
        index =
            (unsigned int)y * WIDTH
            + WIDTH / 2u;

        framebuffer[index] = WHITE;

        y += 4;
    }
}


/* ------------------------------------------------------------------------- */
/* Keyboard                                                                  */
/* ------------------------------------------------------------------------- */

static void set_key_state(
    unsigned int ascii,
    unsigned int down
)
{
    if (ascii == KEY_W)
        w_down = down;
    else if (ascii == KEY_S)
        s_down = down;
    else if (ascii == KEY_O)
        o_down = down;
    else if (ascii == KEY_L)
        l_down = down;
}


/*
 * Drain all queued keyboard events each frame.
 *
 * Event decoding:
 *     bits 0..7 : localized ASCII
 *     bit 8     : key-down flag
 *
 * A zero value means no queued event.
 */
static void poll_keyboard(void)
{
    unsigned int event;
    unsigned int ascii;
    unsigned int down;

    event = keyboard();

    while (event != 0u) {
        ascii =
            event & 0xFFu;

        down =
            (event & 0x100u) != 0u;

        set_key_state(
            ascii,
            down
        );

        event = keyboard();
    }
}


/* ------------------------------------------------------------------------- */
/* Game                                                                      */
/* ------------------------------------------------------------------------- */

static void reset_ball(int direction)
{
    ball_x =
        WIDTH / 2 - BALL_SIZE / 2;

    ball_y =
        HEIGHT / 2 - BALL_SIZE / 2;

    ball_fx =
        ball_x << POS_SHIFT;

    ball_fy =
        ball_y << POS_SHIFT;

    if (direction < 0)
        ball_dx = -BALL_SPEED_X;
    else
        ball_dx = BALL_SPEED_X;

    if (time_low() & 1u)
        ball_dy = BALL_SPEED_Y;
    else
        ball_dy = -BALL_SPEED_Y;
}

static void move_paddles(unsigned int dt_ns)
{
    int delta;

    /*
     * PADDLE_SPEED [pixels/s] * dt [ns], converted to Q16.16.
     * Same time-based movement scheme as the ball.
     */
    delta =
        PADDLE_SPEED
        *
        (int)(dt_ns / 15625u)
        *
        POS_ONE
        /
        64000;


    if (w_down && !s_down)
        left_fy -= delta;
    else if (s_down && !w_down)
        left_fy += delta;

    if (o_down && !l_down)
        right_fy -= delta;
    else if (l_down && !o_down)
        right_fy += delta;


    if (left_fy < 0)
        left_fy = 0;

    if (left_fy > (HEIGHT - PADDLE_H) * POS_ONE)
        left_fy = (HEIGHT - PADDLE_H) * POS_ONE;

    if (right_fy < 0)
        right_fy = 0;

    if (right_fy > (HEIGHT - PADDLE_H) * POS_ONE)
        right_fy = (HEIGHT - PADDLE_H) * POS_ONE;


    left_y =
        left_fy >> POS_SHIFT;

    right_y =
        right_fy >> POS_SHIFT;
}

static void move_ball(unsigned int dt_ns)
{
    int next_fx;
    int next_fy;

    int next_x;
    int next_y;

    int left_x;
    int right_x;

    /*
     * velocity [pixels/s] * dt [ns] * 2^16 / 1e9
     *
     * Split 1e9 as 15625 * 64000.  Dividing dt first keeps every
     * intermediate comfortably within signed 32-bit for our clamped dt.
     *
     * The remainder is intentionally discarded; Q16.16 still gives far more
     * positional precision than this display needs.
     */
    next_fx =
        ball_fx
        +
        ball_dx
        *
        (int)(dt_ns / 15625u)
        *
        POS_ONE
        /
        64000;

    next_fy =
        ball_fy
        +
        ball_dy
        *
        (int)(dt_ns / 15625u)
        *
        POS_ONE
        /
        64000;


    next_x =
        next_fx >> POS_SHIFT;

    next_y =
        next_fy >> POS_SHIFT;


    /*
     * Top/bottom walls.  Reflect the fixed-point position itself so no
     * fractional motion is lost at a bounce.
     */
    if (next_y < 0) {
        next_fy = -next_fy;
        next_y = next_fy >> POS_SHIFT;
        ball_dy = -ball_dy;
    }
    else if (next_y + BALL_SIZE > HEIGHT) {
        next_fy =
            (
                2 * (HEIGHT - BALL_SIZE) * POS_ONE
            )
            -
            next_fy;

        next_y = next_fy >> POS_SHIFT;
        ball_dy = -ball_dy;
    }


    left_x = PADDLE_MARGIN;
    right_x = WIDTH - PADDLE_MARGIN - PADDLE_W;


    /*
     * Left paddle.
     */
    if (
        ball_dx < 0
        &&
        ball_x >= left_x + PADDLE_W
        &&
        next_x < left_x + PADDLE_W
        &&
        next_y + BALL_SIZE > left_y
        &&
        next_y < left_y + PADDLE_H
    ) {
        next_x =
            left_x + PADDLE_W;

        next_fx =
            next_x << POS_SHIFT;

        ball_dx =
            -ball_dx;

        if (
            next_y + BALL_SIZE / 2
            <
            left_y + PADDLE_H / 2
        ) {
            ball_dy = -BALL_SPEED_Y;
        }
        else {
            ball_dy = BALL_SPEED_Y;
        }
    }


    /*
     * Right paddle.
     */
    if (
        ball_dx > 0
        &&
        ball_x + BALL_SIZE <= right_x
        &&
        next_x + BALL_SIZE > right_x
        &&
        next_y + BALL_SIZE > right_y
        &&
        next_y < right_y + PADDLE_H
    ) {
        next_x =
            right_x - BALL_SIZE;

        next_fx =
            next_x << POS_SHIFT;

        ball_dx =
            -ball_dx;

        if (
            next_y + BALL_SIZE / 2
            <
            right_y + PADDLE_H / 2
        ) {
            ball_dy = -BALL_SPEED_Y;
        }
        else {
            ball_dy = BALL_SPEED_Y;
        }
    }


    ball_fx = next_fx;
    ball_fy = next_fy;

    ball_x = next_x;
    ball_y = next_y;


    if (ball_x + BALL_SIZE < 0)
        reset_ball(-1);
    else if (ball_x >= WIDTH)
        reset_ball(1);
}

/* ------------------------------------------------------------------------- */
/* Main                                                                      */
/* ------------------------------------------------------------------------- */

int main(void)
{
    int old_left_y;
    int old_right_y;
    int old_ball_x;
    int old_ball_y;

    left_y =
        HEIGHT / 2 - PADDLE_H / 2;

    right_y =
        HEIGHT / 2 - PADDLE_H / 2;

    left_fy =
        left_y << POS_SHIFT;

    right_fy =
        right_y << POS_SHIFT;

    w_down = 0u;
    s_down = 0u;
    o_down = 0u;
    l_down = 0u;

    reset_ball(1);

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


    draw_center_line();

    fill_rect(
        PADDLE_MARGIN,
        left_y,
        PADDLE_W,
        PADDLE_H
    );

    fill_rect(
        WIDTH - PADDLE_MARGIN - PADDLE_W,
        right_y,
        PADDLE_W,
        PADDLE_H
    );

    fill_rect(
        ball_x,
        ball_y,
        BALL_SIZE,
        BALL_SIZE
    );


    while (1) {
        old_left_y = left_y;
        old_right_y = right_y;
        old_ball_x = ball_x;
        old_ball_y = ball_y;


        poll_keyboard();

        move_paddles(FRAME_DT_NS);
        move_ball(FRAME_DT_NS);


        /*
         * Dirty rectangles only: don't redraw the full framebuffer.
         */
        if (old_left_y != left_y) {
            clear_rect(
                PADDLE_MARGIN,
                old_left_y,
                PADDLE_W,
                PADDLE_H
            );

            fill_rect(
                PADDLE_MARGIN,
                left_y,
                PADDLE_W,
                PADDLE_H
            );
        }


        if (old_right_y != right_y) {
            clear_rect(
                WIDTH - PADDLE_MARGIN - PADDLE_W,
                old_right_y,
                PADDLE_W,
                PADDLE_H
            );

            fill_rect(
                WIDTH - PADDLE_MARGIN - PADDLE_W,
                right_y,
                PADDLE_W,
                PADDLE_H
            );
        }


        clear_rect(
            old_ball_x,
            old_ball_y,
            BALL_SIZE,
            BALL_SIZE
        );

        /*
         * The ball may have erased part of the dotted center line.
         */
        restore_center_line(
            old_ball_x,
            old_ball_y,
            BALL_SIZE,
            BALL_SIZE
        );

        fill_rect(
            ball_x,
            ball_y,
            BALL_SIZE,
            BALL_SIZE
        );


        /*
         * Re-submit so screen implementations that refresh on screen(1, ...)
         * visibly update.
         */
        screen(
            1u,
            (unsigned int)framebuffer
        );
    }


    return 0;
}
