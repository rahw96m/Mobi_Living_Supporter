import sys
import argparse
from delivery_manager import DeliveryManager, DeliveryTask

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
       Mabinogi Mobile - 주간 납품 & 제작 도우미 (AI Connector)
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
        print("\n[메뉴를 선택해주세요]")
        print("1. 🎯 현재 수락된 주간 의뢰/납품 퀘스트 자동 감지 & 제작")
        print("2. ✍️ 아이템 이름 & 수량 직접 입력하여 제작 (예: 로터스 힐링 완드 6개)")
        print("3. 🔍 제작/가공/채집 레시피 검색")
        print("4. 🌐 웹 대시보드(Web UI) 실행 (브라우저로 편리하게 사용)")
        print("5. ⚡ 7슬롯 최고 레벨 가공대 빠른 실행 (수령 ➔ 원자재 일괄 채집 ➔ 고티어 가공)")
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
            print(f"\n🔍 '{target_cat}' 가공대 상태 및 재료 분석 중...")
            try:
                plan = dm.analyze_quick_alter(target_cat if target_cat != "all" else None)
                print("\n=======================================================")
                print("📋 [7슬롯 빠른 가공 분석 결과]")
                print("=======================================================")
                for cat, c_info in plan["categories"].items():
                    print(f"\n[{cat} ({c_info['facility']})]")
                    print(f" • 완료된 작업 (즉시 수령): {c_info['completed_count']}건")
                    print(f" • 가용 슬롯: {c_info['available_slots']}/7")
                    print(f" • 고티어 우선 가공 예정 ({len(c_info['planned_tiers'])}건):")
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

        elif choice.lower() in ("q", "quit", "exit"):
            print("프로그램을 종료합니다.")
            break

def main():
    parser = argparse.ArgumentParser(description="Mabinogi Mobile Delivery Helper")
    parser.add_argument("item", nargs="?", help="제작할 아이템 이름")
    parser.add_argument("count", nargs="?", type=int, default=1, help="제작할 수량")
    parser.add_argument("--web", action="store_true", help="웹 UI 실행")
    parser.add_argument("--quick-alter", nargs="?", const="all", choices=["all", "금속", "목재", "가죽", "옷감"], help="7슬롯 최고 레벨 가공대 빠른 실행")
    args = parser.parse_args()

    dm = DeliveryManager()

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

