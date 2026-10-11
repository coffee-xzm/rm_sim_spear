#include "RuneMenuWidget.h"

#include "RMSimWorldSubsystem.h"
#include "RunePlayerController.h"
#include "Widgets/Input/SButton.h"
#include "Widgets/Layout/SBorder.h"
#include "Widgets/Layout/SBox.h"
#include "Widgets/SBoxPanel.h"
#include "Widgets/Text/STextBlock.h"

void URuneMenuWidget::SetSceneMode(ESceneMode NewMode)
{
	if (UWorld* World = GetWorld())
	{
		if (URMSimWorldSubsystem* Subsystem = World->GetSubsystem<URMSimWorldSubsystem>())
		{
			if (Subsystem->SetSceneMode(NewMode, Subsystem->GetSnapshot().MatchSeed))
			{
				CloseMenu();
			}
		}
	}
}

void URuneMenuWidget::SetCameraView(ECameraView NewView)
{
	if (ARunePlayerController* Controller = Cast<ARunePlayerController>(GetOwningPlayer()))
	{
		if (Controller->SetCameraView(NewView))
		{
			CloseMenu();
		}
	}
}

void URuneMenuWidget::ResetMatch()
{
	if (UWorld* World = GetWorld())
	{
		if (URMSimWorldSubsystem* Subsystem = World->GetSubsystem<URMSimWorldSubsystem>())
		{
			if (Subsystem->ResetMatch(Subsystem->GetSnapshot().MatchSeed))
			{
				CloseMenu();
			}
		}
	}
}

void URuneMenuWidget::ToggleDebugOverlay()
{
	if (UWorld* World = GetWorld())
	{
		if (URMSimWorldSubsystem* Subsystem = World->GetSubsystem<URMSimWorldSubsystem>())
		{
			if (Subsystem->SetDebugOverlay(!Subsystem->GetSnapshot().bDebugOverlay))
			{
				CloseMenu();
			}
		}
	}
}

void URuneMenuWidget::ToggleAutoHitTest()
{
	if (UWorld* World = GetWorld())
	{
		if (URMSimWorldSubsystem* Subsystem = World->GetSubsystem<URMSimWorldSubsystem>())
		{
			const bool bEnabled = Subsystem->GetSnapshot().bAutoHitTestEnabled;
			if (Subsystem->SetAutoHitTestEnabled(!bEnabled))
			{
				CloseMenu();
			}
		}
	}
}

FText URuneMenuWidget::GetStatusText() const
{
	const UWorld* World = GetWorld();
	const URMSimWorldSubsystem* Subsystem = World ? World->GetSubsystem<URMSimWorldSubsystem>() : nullptr;
	const FRunePairSnapshot Snapshot = Subsystem ? Subsystem->GetSnapshot() : FRunePairSnapshot();
	return FText::FromString(Snapshot.Error.IsEmpty()
		? FString::Printf(TEXT("Mode %d | Red %d/5 | Blue %d/5 | Debug %s | AutoHit %s"),
			static_cast<int32>(Snapshot.Mode), Snapshot.Red.ActivatedGroups, Snapshot.Blue.ActivatedGroups,
			Snapshot.bDebugOverlay ? TEXT("On") : TEXT("Off"),
			Snapshot.bAutoHitTestEnabled ? TEXT("On") : TEXT("Off"))
		: Snapshot.Error);
}

void URuneMenuWidget::CloseMenu()
{
	if (ARunePlayerController* Controller = Cast<ARunePlayerController>(GetOwningPlayer()))
	{
		Controller->SetUIMode(false);
	}
}

TSharedRef<SWidget> URuneMenuWidget::RebuildWidget()
{
	auto MakeButton = [](const FString& Label, TFunction<FReply()> Handler)
	{
		return SNew(SButton)
			.ContentPadding(FMargin(12.0f, 7.0f))
			.OnClicked_Lambda(MoveTemp(Handler))
			[SNew(STextBlock).Text(FText::FromString(Label))];
	};

	return SNew(SBox)
		.WidthOverride(360.0f)
		.HAlign(HAlign_Center)
		.VAlign(VAlign_Center)
		[
			SNew(SBorder)
			.Padding(16.0f)
			[
				SNew(SVerticalBox)
				+ SVerticalBox::Slot().AutoHeight().Padding(0, 0, 0, 10)
				[SNew(STextBlock).Text(FText::FromString(TEXT("rm_sim")))]
				+ SVerticalBox::Slot().AutoHeight().Padding(0, 0, 0, 10)
				[SNew(STextBlock).Text_UObject(this, &URuneMenuWidget::GetStatusText).AutoWrapText(true)]
				+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
				[MakeButton(TEXT("未激活"), [this]() { SetSceneMode(ESceneMode::Inactive); return FReply::Handled(); })]
				+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
				[MakeButton(TEXT("小能量机关"), [this]() { SetSceneMode(ESceneMode::SmallRune); return FReply::Handled(); })]
				+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
				[MakeButton(TEXT("大能量机关"), [this]() { SetSceneMode(ESceneMode::LargeRune); return FReply::Handled(); })]
				+ SVerticalBox::Slot().AutoHeight().Padding(0, 10, 0, 2)
				[MakeButton(TEXT("自由视角"), [this]() { SetCameraView(ECameraView::FreeThirdPerson); return FReply::Handled(); })]
				+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
				[MakeButton(TEXT("固定相机"), [this]() { SetCameraView(ECameraView::FixedCamera); return FReply::Handled(); })]
				+ SVerticalBox::Slot().AutoHeight().Padding(0, 10, 0, 2)
				[MakeButton(TEXT("重置"), [this]() { ResetMatch(); return FReply::Handled(); })]
				+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
				[MakeButton(TEXT("调试显示"), [this]() { ToggleDebugOverlay(); return FReply::Handled(); })]
				+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
				[MakeButton(TEXT("自动命中测试"), [this]() { ToggleAutoHitTest(); return FReply::Handled(); })]
				+ SVerticalBox::Slot().AutoHeight().Padding(0, 2)
				[MakeButton(TEXT("关闭"), [this]() { CloseMenu(); return FReply::Handled(); })]
			]
		];
}
