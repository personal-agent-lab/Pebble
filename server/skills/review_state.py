"""Skill 复盘游标的事务内更新，供前台写入与后台复盘共用。"""


def reset_after_user_write(conn) -> None:
    maximum = conn.execute("SELECT COALESCE(MAX(seq),0) FROM skill_review_turns").fetchone()[0]
    conn.execute(
        "UPDATE skill_review_state SET cursor_seq=?, generation=generation+1 WHERE id=1",
        (maximum,),
    )
