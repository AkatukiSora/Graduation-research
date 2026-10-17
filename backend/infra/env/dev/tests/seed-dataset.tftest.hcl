mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["ap-northeast-3a", "ap-northeast-3b"]
    }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  app_image_uri = "example.invalid/repo@sha256:0000000000000000000000000000000000000000000000000000000000000000"
}

run "default_pair" {
  command = plan
  assert {
    condition     = output.stop_point_dataset_id == "nagoya-rule-test-202602-v1"
    error_message = "Unexpected API dataset selector"
  }
}

run "mismatched_seed_is_rejected" {
  command = plan
  variables {
    seed_dataset_ids = "synthetic-v1"
  }
  expect_failures = [var.stop_point_dataset_id]
}
