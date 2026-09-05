from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0003_event_category_event_cover_color_event_cover_image_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='image',
            field=models.ImageField(blank=True, null=True, upload_to='events/'),
        ),
        migrations.AddField(
            model_name='venue',
            name='image',
            field=models.ImageField(blank=True, null=True, upload_to='venues/'),
        ),
    ]
