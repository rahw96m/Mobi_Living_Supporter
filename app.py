import sys
import argparse
from delivery_manager import DeliveryManager, DeliveryTask, settings_store

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

def print_banner():
    banner = """
===============================================================
               모비노기 생활 지원도구 (AI Connector)
===============================================================
"""
    print(banner)

def show_character_status(dm: DeliveryManager):
    print("--- [현재 캐릭터 및 게임 상태] ---")
    try:
        info = dm.cli.get_my_info()
        env = dm.cli.get_current_environment()
        wings = dm.cli.get_wings_count()
        inv = dm.cli.get_inventory()
        cur_wt = inv.get("CurrentInventoryWeightAsDecimal", 0)
        max_wt = inv.get("MaxInventoryWeightAsDecimal", 0)

        print(f"• 서버/채널: {info.get('RealmName', '-')} / {env.get('GameSpaceDisplayName', '-')}")
        print(f"• 직업/레벨: Lv.{info.get('Level', 0)} {info.get('EnabledCombatJobDisplayName', '-')} (칭호: {info.get('Title', '-')})")
        print(f"• 정령의 날개: {wings:,}개")
        print(f"• 가방 무게: {cur_wt} / {max_wt}")
    except Exception as ex:
        print(f"⚠️ 상태 조회 실패: {ex}")
    print("-----------------------------------")

def run_interactive(dm: DeliveryManager):
    print_banner()
    show_character_status(dm)

    while True:
        curr_order = settings_store.get("alter_order", "high_tier")
        order_str = "상위 티어 우선 (오래 걸리는 가공부터)" if curr_order != "low_tier" else "하위 티어 우선 (기초 재료부터)"
        curr_wool = settings_store.get("wool_gather_order", "high_tier")
        wool_labels = {
            "high_tier": "상위 양 우선 (먹구름>곱슬>양, 단계적 폴백)",
            "drop_rate": "드롭 효율 최적화 (품목별 전담 양 우선)",
            "low_tier": "일반 양 우선 (초보자 권장)"
        }
        wool_str = wool_labels.get(curr_wool, curr_wool)
        curr_wood = settings_store.get("wood_gather_order", "drop_rate")
        wood_labels = {
            "drop_rate": "드롭 효율 최적화 (진액=뾰족 나무, 통나무=굵은 나무)",
            "high_tier": "상위 나무 우선 (상급 나무+>상급 나무>굵은>뾰족)",
            "low_tier": "기본 나무 우선 (굵은>뾰족>상급)"
        }
        wood_str = wood_labels.get(curr_wood, curr_wood)

        print("\n[메뉴를 선택해주세요]")
        print("1. 🎯 현재 수락된 주간 의뢰/납품 퀘스트 자동 감지 & 제작")
        print("2. ✍️ 아이템 이름 & 수량 직접 입력하여 제작 (예: 로터스 힐링 완드 6개)")
        print("3. 🔍 제작/가공/채집 레시피 검색")
        print("4. 🌐 웹 대시보드(Web UI) 실행 (브라우저로 편리하게 사용)")
        print(f"5. ⚡ 7슬롯 최고 레벨 가공대 빠른 실행 [{order_str}]")
        print(f"6. ⚙️ 가공 우선순위 설정 변경 (현재: {order_str})")
        print(f"7. 🧶 양털 채집 우선순위 설정 변경 (현재: {wool_str})")
        print(f"8. 🪓 벌목 채집 우선순위 설정 변경 (현재: {wood_str})")
        print("q. 종료")

        choice = input("\n선택 > ").strip()

        if choice == "1":
            tasks = dm.detect_delivery_quests()
            if not tasks:
                print("\n⚠️ 현재 퀘스트 목록에 납품 관련 목표(예: '〇〇 보유 X/Y')가 없습니다.")
                continue

            print(f"\n📋 [감지된 납품 퀘스트: 총 {len(tasks)}건]")
            for idx, t in enumerate(tasks, start=1):
                status_str = "✅ 완료됨" if t.is_completed else f"⏳ {t.needed}개 부족"
                print(f"  [{idx}] {t.quest_title} -> {t.item_name} (현재: {t.current}/{t.goal} | {status_str})")

            sel = input("\n제작할 퀘스트 번호를 입력하세요 (취소: 0) > ").strip()
            if not sel.isdigit() or int(sel) <= 0 or int(sel) > len(tasks):
                print("취소되었습니다.")
                continue

            selected_task = tasks[int(sel) - 1]
            if selected_task.is_completed:
                print(f"\n'{selected_task.item_name}'은(는) 이미 목표 수량을 달성했습니다! 게시판에 납품해 주세요.")
                continue

            confirm = input(f"\n'{selected_task.item_name}' {selected_task.goal}개(부족: {selected_task.needed}개) 제작을 시작할까요? (y/n) > ").strip().lower()
            if confirm == "y":
                try:
                    dm.resolve_and_produce(selected_task.item_name, selected_task.goal)
                except Exception as ex:
                    print(f"\n❌ 작업 중 오류 발생: {ex}")

        elif choice == "2":
            item_name = input("\n제작할 아이템 이름을 입력하세요 (예: 로터스 힐링 완드) > ").strip()
            if not item_name:
                print("아이템 이름을 입력해야 합니다.")
                continue

            count_str = input("목표 수량을 입력하세요 (기본 1) > ").strip()
            count = int(count_str) if count_str.isdigit() and int(count_str) > 0 else 1

            confirm = input(f"\n'{item_name}' {count}개 제작을 시작할까요? (y/n) > ").strip().lower()
            if confirm == "y":
                try:
                    dm.resolve_and_produce(item_name, count)
                except Exception as ex:
                    print(f"\n❌ 작업 중 오류 발생: {ex}")

        elif choice == "3":
            query = input("\n검색할 아이템 또는 재료 이름 > ").strip()
            if not query:
                continue
            crafts = dm.cli.get_craftable_items(query).get("items", [])
            alters = dm.cli.get_alterable_items(query).get("items", [])
            gaths = dm.cli.get_gatherable_items(query).get("items", [])

            print(f"\n--- 검색 결과: '{query}' ---")
            if crafts:
                print(f"[제작 레시피 ({len(crafts)}건)]")
                for c in crafts:
                    craftable_str = "제작 가능" if c.get("Craftable") else f"불가 ({c.get('Reason')})"
                    print(f"  • {c.get('DisplayName')} - {craftable_str}")
            if alters:
                print(f"[가공 레시피 ({len(alters)}건)]")
                for a in alters:
                    alter_str = "가공 가능" if a.get("Alterable") else f"불가 ({a.get('Reason')})"
                    print(f"  • {a.get('DisplayName')} - {alter_str}")
            if gaths:
                print(f"[필드 채집 ({len(gaths)}건)]")
                for g in gaths:
                    tool_str = "도구 준비완료" if g.get("ToolOk") else "도구 없음/내구도0"
                    print(f"  • {g.get('DisplayName')} - {tool_str}")

        elif choice == "4":
            print("\n웹 대시보드를 실행합니다...")
            from web_server import start_server
            start_server()
            break

        elif choice == "5":
            print("\n[7슬롯 최고 레벨 가공대 빠른 실행]")
            print(f"ℹ️ 가공 우선순위: {order_str}")
            print("1. ⚡ 전체 가공대 일괄 (금속, 목재, 가죽, 옷감)")
            print("2. 🪙 금속 가공 시설 (백금강괴 ~ 철괴)")
            print("3. 🪵 목재 가공 시설 (특급 목재 ~ 목재)")
            print("4. 🦊 가죽 가공 시설 (특급 가죽 ~ 가죽)")
            print("5. 🧶 옷감 가공 시설 (특급 옷감 ~ 옷감)")
            print("0. 취소")

            c_sel = input("\n가공 종류 선택 > ").strip()
            cat_map = {"1": "all", "2": "금속", "3": "목재", "4": "가죽", "5": "옷감"}
            if c_sel not in cat_map:
                print("취소되었습니다.")
                continue

            target_cat = cat_map[c_sel]
            print(f"\n🔍 '{target_cat}' 가공대 상태 및 재료 분석 중... ({order_str})")
            try:
                plan = dm.analyze_quick_alter(target_cat if target_cat != "all" else None)
                print("\n=======================================================")
                print(f"📋 [7슬롯 빠른 가공 분석 결과 ({order_str})]")
                print("=======================================================")
                for cat, c_info in plan["categories"].items():
                    print(f"\n[{cat} ({c_info['facility']})]")
                    print(f" • 완료된 작업 (즉시 수령): {c_info['completed_count']}건")
                    print(f" • 가용 슬롯: {c_info['available_slots']}/7")
                    print(f" • 가공 예정 ({len(c_info['planned_tiers'])}건):")
                    for p in c_info["planned_tiers"]:
                        g_str = f" [채집 필요: {p['gather_reqs']}]" if p["needs_gathering"] else " [즉시 가능]"
                        print(f"    - Tier {p['tier']} {p['item_name']} (레시피: {p['recipe_name']}){g_str}")
                    if c_info["skipped_tiers"]:
                        print(f" • 건너뛴 티어 ({len(c_info['skipped_tiers'])}건):")
                        for s in c_info["skipped_tiers"]:
                            print(f"    - Tier {s['tier']} {s['item_name']}: {s['reason']}")

                if plan["total_raw_needed"]:
                    print(f"\n🌿 [사전 맞춤 채집 필요 원자재 (부족 수량만 자동 채집)]")
                    for r_name, r_qty in plan["total_raw_needed"].items():
                        print(f" • {r_name}: {r_qty}개")
                else:
                    print(f"\n✨ 사전 채집이 필요한 원자재가 없습니다 (보유량 충분).")

                if not plan["can_start"]:
                    print("\n⚠️ 수령할 완료품이 없거나 가공 가능한 티어가 없습니다.")
                    continue

                conf = input("\n위 계획대로 [수령 ➔ 채집 ➔ 가공]을 시작할까요? (y/n) > ").strip().lower()
                if conf == "y":
                    dm.execute_quick_alter(plan)
            except Exception as ex:
                print(f"\n❌ 실행 중 오류 발생: {ex}")

        elif choice == "6":
            curr = settings_store.get("alter_order", "high_tier")
            new_val = "low_tier" if curr == "high_tier" else "high_tier"
            settings_store.set("alter_order", new_val)
            new_str = "상위 티어 우선 (오래 걸리는 가공부터)" if new_val != "low_tier" else "하위 티어 우선 (기초 재료부터)"
            print(f"\n✅ 가공 우선순위가 변경되었습니다: {new_str}")

        elif choice == "7":
            curr = settings_store.get("wool_gather_order", "high_tier")
            order_cycle = ["high_tier", "drop_rate", "low_tier"]
            next_idx = (order_cycle.index(curr) + 1) % len(order_cycle) if curr in order_cycle else 0
            new_val = order_cycle[next_idx]
            settings_store.set("wool_gather_order", new_val)
            wool_labels = {
                "high_tier": "🥇 상위 양 우선 (먹구름>곱슬>양, 단계적 폴백)",
                "drop_rate": "⚖️ 드롭 효율 최적화 (품목별 전담 양 우선)",
                "low_tier": "🥉 일반 양 우선 (초보자 권장)"
            }
            print(f"\n✅ 양털 채집 우선순위가 변경되었습니다: {wool_labels.get(new_val, new_val)}")

        elif choice == "8":
            curr = settings_store.get("wood_gather_order", "drop_rate")
            order_cycle = ["drop_rate", "high_tier", "low_tier"]
            next_idx = (order_cycle.index(curr) + 1) % len(order_cycle) if curr in order_cycle else 0
            new_val = order_cycle[next_idx]
            settings_store.set("wood_gather_order", new_val)
            wood_labels = {
                "drop_rate": "⚖️ 드롭 효율 최적화 (진액=뾰족 나무, 통나무=굵은 나무)",
                "high_tier": "🥇 상위 나무 우선 (상급 나무+>상급 나무>굵은>뾰족)",
                "low_tier": "🥉 기본 나무 우선 (굵은>뾰족>상급)"
            }
            print(f"\n✅ 벌목 채집 우선순위가 변경되었습니다: {wood_labels.get(new_val, new_val)}")

        elif choice.lower() in ("q", "quit", "exit"):
            print("프로그램을 종료합니다.")
            break

def main():
    parser = argparse.ArgumentParser(description="모비노기 생활 지원도구")
    parser.add_argument("item", nargs="?", help="제작할 아이템 이름")
    parser.add_argument("count", nargs="?", type=int, default=1, help="제작할 수량")
    parser.add_argument("--web", action="store_true", help="웹 UI 실행")
    parser.add_argument("--quick-alter", nargs="?", const="all", choices=["all", "금속", "목재", "가죽", "옷감"], help="7슬롯 최고 레벨 가공대 빠른 실행")
    parser.add_argument("--alter-order", choices=["high", "low", "high_tier", "low_tier"], help="가공 우선순위 (high: 상위 티어부터 / low: 하위 티어부터)")
    parser.add_argument("--wool-order", choices=["high", "drop", "low", "high_tier", "drop_rate", "low_tier"], help="양털 채집 우선순위 (high: 상위 양 우선 / drop: 드롭 효율 최적화 / low: 일반 양 우선)")
    parser.add_argument("--wood-order", choices=["drop", "high", "low", "drop_rate", "high_tier", "low_tier"], help="벌목 채집 우선순위 (drop: 드롭 효율 최적화 / high: 상위 나무 우선 / low: 기본 나무 우선)")
    args = parser.parse_args()

    dm = DeliveryManager()

    if args.alter_order:
        norm = "low_tier" if "low" in args.alter_order else "high_tier"
        settings_store.set("alter_order", norm)
        print(f"⚙️ 가공 우선순위 설정: {'상위 티어 우선' if norm == 'high_tier' else '하위 티어 우선'}")

    if args.wool_order:
        norm_w = "drop_rate" if "drop" in args.wool_order else ("low_tier" if "low" in args.wool_order else "high_tier")
        settings_store.set("wool_gather_order", norm_w)
        w_labels = {"high_tier": "상위 양 우선", "drop_rate": "드롭 효율 최적화", "low_tier": "일반 양 우선"}
        print(f"🧶 양털 채집 우선순위 설정: {w_labels.get(norm_w, norm_w)}")

    if args.wood_order:
        norm_wd = "high_tier" if "high" in args.wood_order else ("low_tier" if "low" in args.wood_order else "drop_rate")
        settings_store.set("wood_gather_order", norm_wd)
        wd_labels = {"drop_rate": "드롭 효율 최적화 (진액=뾰족, 통나무=굵은)", "high_tier": "상위 나무 우선", "low_tier": "기본 나무 우선"}
        print(f"🪓 벌목 채집 우선순위 설정: {wd_labels.get(norm_wd, norm_wd)}")

    if args.web:
        from web_server import start_server
        start_server()
    elif args.quick_alter:
        print_banner()
        show_character_status(dm)
        cat = None if args.quick_alter == "all" else args.quick_alter
        plan = dm.analyze_quick_alter(cat)
        dm.execute_quick_alter(plan)
    elif args.item:
        print_banner()
        dm.resolve_and_produce(args.item, args.count)
    else:
        run_interactive(dm)

if __name__ == "__main__":
    main()

